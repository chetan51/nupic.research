# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2019, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

from collections import OrderedDict

import torch
from torch import nn

from nupic.torch.models.sparse_cnn import GSCSparseCNN, MNISTSparseCNN
from nupic.torch.modules import Flatten, KWinners, KWinners2d

from .layers import DSConv2d, DynamicSparseBase, RandDSConv2d, SparseConv2d
from .main import VGG19

# redefine Flatten
# class Lambda(nn.Module):
#     def __init__(self, func:LambdaFunc):
#         super().__init__()
#         self.func = func

#     def forward(self, x):
#         return self.func(x)

# def Flatten():
#     return Lambda(lambda x: x.view((x.size(0), -1)))


# ----------------------------------------
# Utils - network builders and inspectors
# ----------------------------------------

def get_dynamic_sparse_modules(net):
    """
    Inspects all children recursively to collect the
    Dynamic-Sparse modules.
    """
    sparse_modules = []
    for module in net.modules():

        if isinstance(module, DynamicSparseBase):
            sparse_modules.append(module)

    return sparse_modules


def swap_layers(sequential, layer_type_a, layer_type_b):
    """
    If 'layer_type_a' appears immediately before 'layer_type_2',
    this function will swap their position in a new sequential.

    :param sequential: torch.nn.Sequential
    :param layer_type_a: type of first layer
    :param layer_type_a: type of second layer
    """

    old_seq = dict(sequential.named_children())
    names = list(old_seq.keys())
    modules = list(old_seq.values())

    # Make copy of sequence.
    new_seq = list(old_seq.items())

    # Edit copy in place.
    layer_a = modules[0]
    name_a = names[0]
    for i, (name_b, layer_b) in enumerate(list(old_seq.items())[1:], 1):

        if isinstance(layer_a, layer_type_a) and isinstance(layer_b, layer_type_b):
            new_seq[i - 1] = (name_b, layer_b)
            new_seq[i] = (name_a, layer_a)

        layer_a = layer_b
        name_a = name_b

    # Turn sequence into nn.Sequential.
    new_seq = OrderedDict(new_seq)
    new_seq = torch.nn.Sequential(new_seq)
    return new_seq


def squash_layers(sequential, *types):
    """
    This function squashes layers matching the sequence of 'types'.
    For instance, if 'types' is [Conv2d, BatchNorm, KWinners] and
    "sequential" has layers [..., Conv2d, BatchNorm, KWinners, ...],
    then a new "sequential" will be returns of the form
    [..., SubSequence, ...] where SubSequence calls .

    More importantly, SubSequence will use the same hook (if any)
    as the original Conv2d, although with the output from KWinners
    - at least in this example case.

    :param sequential: torch.nn.Sequential
    :param types: types of layers

    :returns: a new torch.nn.Sequential
    """
    assert len(types) <= len(sequential), "More types than layers passed."
    assert len(types) > 1, "Expected more than one type to squash."

    named_children = dict(sequential.named_children())
    names = list(named_children.keys())
    modules = list(named_children.values())

    i0 = 0
    new_seq = []

    while i0 < len(modules):

        i1 = i0 + len(types)
        if i1 > len(modules) + 1:
            break

        sublayers = modules[i0:i1]
        subnames = names[i0:i1]
        matches = [isinstance(layer, ltype) for layer, ltype in zip(sublayers, types)]
        if all(matches):

            # Save forward hook of base layer.
            base_layer = modules[i0]
            if hasattr(base_layer, "forward_hook"):
                forward_hook = base_layer.forward_hook
                if hasattr(base_layer, "forward_hook_handle"):
                    base_layer.forward_hook_handle.remove()
            else:
                forward_hook = None

            # Squash layers.
            squashed = OrderedDict(zip(subnames, sublayers))
            squashed = torch.nn.Sequential(squashed)
            assert squashed[0] == base_layer

            # Maintain same forward hook.
            if forward_hook:
                forward_hook_handle = squashed.register_forward_hook(
                    lambda module, in_, out_:
                    forward_hook(module[0], in_, out_)
                )
                squashed.forward_hook = forward_hook
                squashed.forward_hook_handle = forward_hook_handle

            # Append squashed sequence
            name = "squashed" + str(i0)
            new_seq.append((name, squashed))

            # Iterate i0.
            i0 = i1

        else:

            # Append layer as is.
            name = names[i0]
            module = modules[i0]
            new_seq.append((name, module))

            # Iterate i0.
            i0 += 1

    # Turn sequence into nn.Sequential.
    new_seq = OrderedDict(new_seq)
    new_seq = torch.nn.Sequential(new_seq)
    return new_seq


def set_module(net, name, new_module):
    """
    Mimics "setattr" in purpose and argument types.
    Sets module "name" of "net" to "new_module".
    This is done recursively as "name" may be
    of the form '0.subname-1.subname-2.3 ...'
    where 0 and 3 indicate indices of a
    torch.nn.Sequential.
    """

    subnames = name.split(".")
    subname0, subnames_remaining = subnames[0], subnames[1:]

    if subnames_remaining:

        if subname0.isdigit():
            subnet = net[int(subname0)]
        else:
            subnet = getattr(net, subname0)

        set_module(subnet, ".".join(subnames_remaining), new_module)

    else:

        if subname0.isdigit():
            net[int(subname0)] = new_module
        else:
            setattr(net, subname0, new_module)


# --------------
# GSC Networks
# --------------

class GSCHeb(nn.Module):
    """LeNet like CNN used for GSC in how so dense paper."""

    def __init__(self, config=None):
        super(GSCHeb, self).__init__()

        defaults = dict(
            input_size=1024,
            num_classes=12,
            boost_strength=1.5,
            boost_strength_factor=0.9,
            k_inference_factor=1.5,
            duty_cycle_period=1000,
            use_kwinners=True,
            hidden_neurons_fc=1000,
        )
        defaults.update(config or {})
        self.__dict__.update(defaults)
        self.device = torch.device(self.device)

        if self.model == "DSNNMixedHeb":
            self.hebbian_learning = True
        else:
            self.hebbian_learning = False

        # hidden layers
        conv_layers = [
            *self._conv_block(1, 64, percent_on=0.095),  # 28x28 -> 14x14
            *self._conv_block(64, 64, percent_on=0.125),  # 10x10 -> 5x5
        ]
        linear_layers = [
            Flatten(),
            # *self._linear_block(1600, 1500, percent_on= 0.067),
            *self._linear_block(1600, self.hidden_neurons_fc, percent_on=0.1),
            nn.Linear(self.hidden_neurons_fc, self.num_classes),
        ]

        # classifier (*redundancy on layers to facilitate traversing)
        self.layers = conv_layers + linear_layers
        self.features = nn.Sequential(*conv_layers)
        self.classifier = nn.Sequential(*linear_layers)

        # track correlations
        self.correlations = []

    def _conv_block(self, fin, fout, percent_on=0.1):
        block = [
            nn.Conv2d(fin, fout, kernel_size=5, stride=1, padding=0),
            nn.BatchNorm2d(fout, affine=False),
            nn.MaxPool2d(kernel_size=2, stride=2),
            self._activation_func(fout, percent_on),
        ]
        # if not self.use_kwinners:
        #     block.append(nn.Dropout(p=0.5))
        return block

    def _linear_block(self, fin, fout, percent_on=0.1):
        block = [
            nn.Linear(fin, fout),
            nn.BatchNorm1d(fout, affine=False),
            self._activation_func(fout, percent_on, twod=False),
        ]
        # if not self.use_kwinners:
        #     block.append(nn.Dropout(p=0.5))
        return block

    def _activation_func(self, fout, percent_on, twod=True):
        if self.use_kwinners:
            if twod:
                activation_func = KWinners2d
            else:
                activation_func = KWinners
            return activation_func(
                fout,
                percent_on=percent_on,
                boost_strength=self.boost_strength,
                boost_strength_factor=self.boost_strength_factor,
                k_inference_factor=self.k_inference_factor,
                duty_cycle_period=self.duty_cycle_period,
            )
        else:
            return nn.ReLU()

    def _has_activation(self, idx, layer):
        return idx == len(self.layers) - 1 or isinstance(layer, KWinners)

    def forward(self, x):
        """A faster and approximate way to track correlations"""
        # Forward pass through conv layers
        for layer in self.features:
            x = layer(x)

        # Forward pass through linear layers
        idx_activation = 0
        for layer in self.classifier:
            # do the forward calculation normally
            x = layer(x)
            if self.hebbian_learning:
                if isinstance(layer, Flatten):
                    prev_act = (x > 0).detach().float()
                if isinstance(layer, KWinners):
                    n_samples = x.shape[0]
                    with torch.no_grad():
                        curr_act = (x > 0).detach().float()
                        # add outer product to the correlations, per sample
                        for s in range(n_samples):
                            outer = torch.ger(prev_act[s], curr_act[s])
                            if idx_activation + 1 > len(self.correlations):
                                self.correlations.append(outer)
                            else:
                                self.correlations[idx_activation] += outer
                        # reassigning to the next
                        prev_act = curr_act
                        # move to next activation
                        idx_activation += 1

        return x


# make a conv heb just by replacing the conv layers by special DSNN Conv layers
def gsc_conv_heb(config):

    net = make_dscnn(GSCHeb(config), config)
    net.dynamic_sparse_modules = get_dynamic_sparse_modules(net)

    return net


def gsc_conv_only_heb(config):
    network = make_dscnn(GSCHeb(config), config)

    # replace the forward function to not apply regular convolution
    def forward(self, x):
        return self.classifier(self.features(x))

    network.forward = forward
    network.dynamic_sparse_modules = get_dynamic_sparse_modules(network)

    return network


# function that makes the switch
# why function inside other functions -> make it into a class?

def make_dscnn(net, config=None):
    """
    Edits net in place to replace Conv2d layers with those
    specified in config.
    """

    config = config or {}

    named_convs = [
        (name, layer)
        for name, layer in net.named_modules()
        if isinstance(layer, torch.nn.Conv2d)
    ]
    num_convs = len(named_convs)

    def tolist(param):
        if isinstance(param, list):
            return param
        else:
            return [param] * num_convs

    def get_conv_type(prune_method):
        if prune_method == "random":
            return RandDSConv2d
        elif prune_method == "static":
            return SparseConv2d
        elif prune_method == "dynamic":
            return DSConv2d

    # Get DSConv2d params from config.
    prune_methods = tolist(config.get("prune_methods", "dynamic"))
    assert (
        len(prune_methods) == num_convs
    ), "Not enough prune_methods specified in config. Expected {}, got {}".format(
        num_convs, prune_methods
    )

    # Populate kwargs for new layers.
    possible_args = {
        "dynamic": [
            "hebbian_prune_frac",
            "weight_prune_frac",
            "sparsity",
            "prune_dims",
            "update_nsteps",
        ],
        "random": [
            "hebbian_prune_frac",
            "weight_prune_frac",
            "sparsity",
            "prune_dims",
            "update_nsteps",
        ],
        "static": ["sparsity"],
        None: [],
    }
    kwargs_s = []
    for c_i in range(num_convs):
        layer_args = {}
        prune_method = prune_methods[c_i]
        for arg in possible_args[prune_method]:
            if arg in config:
                layer_args[arg] = tolist(config.get(arg))[c_i]
        kwargs_s.append(layer_args)

    assert (
        len((kwargs_s)) == len(named_convs) == len(prune_methods)
    ), "Sizes do not match"

    # Replace conv layers.
    for prune_method, kwargs, (name, conv) in zip(prune_methods, kwargs_s, named_convs):

        conv_type = get_conv_type(prune_method)
        if conv_type is None:
            continue

        set_module(net, name, conv_type(
            in_channels=conv.in_channels,
            out_channels=conv.out_channels,
            kernel_size=conv.kernel_size,
            stride=conv.stride,
            padding=conv.padding,
            padding_mode=conv.padding_mode,
            dilation=conv.dilation,
            groups=conv.groups,
            bias=(conv.bias is not None),
            **kwargs,
        ))

    return net


def vgg19_dscnn(config):

    net = VGG19(config)
    net = make_dscnn(net)

    net.dynamic_sparse_modules = get_dynamic_sparse_modules(net)

    return net


def mnist_sparse_cnn(config):

    net_params = config.get("net_params", {})
    net = MNISTSparseCNN(**net_params)
    return net


def mnist_sparse_dscnn(config, squash=True):

    net_params = config.get("net_params", {})
    net = MNISTSparseCNN(**net_params)
    net = make_dscnn(net, config)
    net = swap_layers(net, nn.MaxPool2d, KWinners2d)
    net = squash_layers(net, DSConv2d, KWinners2d)

    net.dynamic_sparse_modules = get_dynamic_sparse_modules(net)

    return net


def gsc_sparse_cnn(config):

    net_params = config.get("net_params", {})
    net = GSCSparseCNN(net_params)
    return net


def gsc_sparse_dscnn(config):

    net_params = config.get("net_params", {})
    net = GSCSparseCNN(**net_params)
    net = make_dscnn(net, config)
    net = swap_layers(net, nn.MaxPool2d, KWinners2d)
    net = squash_layers(net, DSConv2d, nn.BatchNorm2d, KWinners2d)

    net.dynamic_sparse_modules = get_dynamic_sparse_modules(net)

    return net


class GSCSparseFullCNN(nn.Sequential):
    """Sparse CNN model used to classify `Google Speech Commands` dataset as
    described in `How Can We Be So Dense?`_ paper.

    .. _`How Can We Be So Dense?`: https://arxiv.org/abs/1903.11257

    :param cnn_out_channels: output channels for each CNN layer
    :param cnn_percent_on: Percent of units allowed to remain on each convolution
                           layer
    :param linear_units: Number of units in the linear layer
    :param linear_percent_on: Percent of units allowed to remain on the linear
                              layer
    :param linear_weight_sparsity: Percent of weights that are allowed to be
                                   non-zero in the linear layer
    :param k_inference_factor: During inference (training=False) we increase
                               `percent_on` in all sparse layers by this factor
    :param boost_strength: boost strength (0.0 implies no boosting)
    :param boost_strength_factor: Boost strength factor to use [0..1]
    :param duty_cycle_period: The period used to calculate duty cycles
    """

    def __init__(self,
                 cnn_out_channels=(32, 64, 32),
                 cnn_percent_on=(0.095, 0.125, 0.0925),
                 linear_units=1600,
                 linear_percent_on=0.1,
                 linear_weight_sparsity=0.4,
                 boost_strength=1.5,
                 boost_strength_factor=0.9,
                 k_inference_factor=1.5,
                 duty_cycle_period=1000
                 ):
        super(GSCSparseFullCNN, self).__init__()
        # input_shape = (1, 32, 32)
        # First Sparse CNN layer
        self.add_module("cnn1", nn.Conv2d(1, cnn_out_channels[0], 5))
        self.add_module("cnn1_batchnorm", nn.BatchNorm2d(cnn_out_channels[0],
                                                         affine=False))
        self.add_module("cnn1_maxpool", nn.MaxPool2d(2))
        self.add_module("cnn1_kwinner", KWinners2d(
            channels=cnn_out_channels[0],
            percent_on=cnn_percent_on[0],
            k_inference_factor=k_inference_factor,
            boost_strength=boost_strength,
            boost_strength_factor=boost_strength_factor,
            duty_cycle_period=duty_cycle_period))

        # Second Sparse CNN layer
        self.add_module("cnn2", nn.Conv2d(cnn_out_channels[0], cnn_out_channels[1], 5))
        self.add_module("cnn2_batchnorm",
                        nn.BatchNorm2d(cnn_out_channels[1], affine=False))
        self.add_module("cnn2_maxpool", nn.MaxPool2d(2))
        self.add_module("cnn2_kwinner", KWinners2d(
            channels=cnn_out_channels[1],
            percent_on=cnn_percent_on[1],
            k_inference_factor=k_inference_factor,
            boost_strength=boost_strength,
            boost_strength_factor=boost_strength_factor,
            duty_cycle_period=duty_cycle_period))

        # # Third Sparse CNN layer
        # self.add_module("cnn3",
        #                 nn.Conv2d(cnn_out_channels[1], cnn_out_channels[2], 5))
        # self.add_module("cnn3_batchnorm",
        #                 nn.BatchNorm2d(cnn_out_channels[2], affine=False))
        # # self.add_module("cnn3_maxpool", nn.MaxPool2d(2))
        # self.add_module("cnn3_kwinner", KWinners2d(
        #     channels=cnn_out_channels[2],
        #     percent_on=cnn_percent_on[2],
        #     k_inference_factor=k_inference_factor,
        #     boost_strength=boost_strength,
        #     boost_strength_factor=boost_strength_factor,
        #     duty_cycle_period=duty_cycle_period))

        self.add_module("flatten", Flatten())

        # # Sparse Linear layer
        # self.add_module("linear", SparseWeights(
        #     nn.Linear(25 * cnn_out_channels[1], linear_units),
        #     weight_sparsity=linear_weight_sparsity))
        # self.add_module("linear_bn", nn.BatchNorm1d(linear_units, affine=False))
        # self.add_module("linear_kwinner", KWinners(
        #     n=linear_units,
        #     percent_on=linear_percent_on,
        #     k_inference_factor=k_inference_factor,
        #     boost_strength=boost_strength,
        #     boost_strength_factor=boost_strength_factor,
        #     duty_cycle_period=duty_cycle_period))

        # Classifier
        self.add_module("output", nn.Linear(1600, 12))
        self.add_module("softmax", nn.LogSoftmax(dim=1))


def gsc_sparse_dscnn_fullyconv(config):

    net_params = config.get("net_params", {})
    net = GSCSparseFullCNN(**net_params)
    net = make_dscnn(net, config)
    net = swap_layers(net, nn.MaxPool2d, KWinners2d)
    net = squash_layers(net, DSConv2d, nn.BatchNorm2d, KWinners2d)

    net.dynamic_sparse_modules = get_dynamic_sparse_modules(net)

    return net
