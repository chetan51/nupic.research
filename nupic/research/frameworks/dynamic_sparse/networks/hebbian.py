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

import torch
from torch import nn

from nupic.torch.modules import KWinners

from .layers import DynamicSparseBase, init_coactivation_tracking

# ------------------------------------------------------------------------------------
# DynamicSparse Linear Block 2019-09-13
# ------------------------------------------------------------------------------------


class DSLinearBlock(nn.Sequential, DynamicSparseBase):

    def __init__(
        self,
        in_features,
        out_features,
        bias,
        batch_norm=None,
        dropout=None,
        activation_func=None,
    ):

        # Clarifications on batch norm position at the linear block:
        # - bn before relu at original paper
        # - bn after relu in recent work
        # (see fchollet @ https://github.com/keras-team/keras/issues/1802)
        # - however, if applied after RELU or kWinners, breaks sparsity
        layers = [nn.Linear(in_features, out_features, bias=bias)]
        if batch_norm:
            layers.append(nn.BatchNorm1d(out_features))
        if activation_func:
            layers.append(activation_func)
        if dropout:
            layers.append(nn.Dropout(p=dropout))
        super().__init__(*layers)

        # Initialize dynamic sparse attributes.
        self._init_coactivations(weight=self[0].weight)

    @property
    def weight(self):
        """
        Return weight of linear layer - needed for introspective networks.
        """
        return self[0].weight

    def forward(self, input_tensor):
        output_tensor = super().forward(input_tensor)
        if self._track_coactivations:
            self.update_coactivations(input_tensor, output_tensor)
        return output_tensor

    def update_coactivations(self, x, y):
        outer = 0
        n_samples = x.shape[0]
        with torch.no_grad():

            # Get active units.
            curr_act = (x > 0).detach().float()
            prev_act = (y > 0).detach().float()

            # Cumulate outer product over all samples.
            # TODO: Vectorize this sum; for instance, using torch.einsum().
            for s in range(n_samples):
                outer += torch.ger(prev_act[s], curr_act[s])

        # Update coactivations.
        self.coactivations[:] += outer


# ------------
# MLP Network
# ------------

class MLPHeb(nn.Module):
    """Simple 3 hidden layers + output MLP"""

    def __init__(self, config=None):
        super().__init__()

        defaults = dict(
            device="cpu",
            input_size=784,
            num_classes=10,
            hidden_sizes=[100, 100, 100],
            percent_on_k_winner=[1.0, 1.0, 1.0],
            boost_strength=[1.4, 1.4, 1.4],
            boost_strength_factor=[0.7, 0.7, 0.7],
            batch_norm=False,
            dropout=False,
            bias=True,
        )
        assert config is None or "use_kwinners" not in config, \
            "use_kwinners is deprecated"

        defaults.update(config or {})
        self.__dict__.update(defaults)
        self.device = torch.device(self.device)

        # decide which actiovation function to use
        self.activation_funcs = []
        for layer, hidden_size in enumerate(self.hidden_sizes):
            if self.percent_on_k_winner[layer] < 0.5:
                self.activation_funcs.append(
                    KWinners(n=hidden_size,
                             percent_on=self.percent_on_k_winner[layer],
                             boost_strength=self.boost_strength[layer],
                             boost_strength_factor=self.boost_strength_factor[layer],
                             k_inference_factor=1.0,
                             )
                )
            else:
                self.activation_funcs.append(
                    nn.ReLU()
                )

        # Construct layers.
        layers = []
        kwargs = dict(
            bias=self.bias,
            batch_norm=self.batch_norm,
            dropout=self.dropout,
        )
        # Flatten image.
        layers = [nn.Flatten()]
        # Add the first layer
        layers.append(
            DSLinearBlock(
                self.input_size,
                self.hidden_sizes[0],
                activation_func=self.activation_funcs[0],
                **kwargs),
        )
        # Add hidden layers.
        for i in range(1, len(self.hidden_sizes)):
            layers.append(
                DSLinearBlock(
                    self.hidden_sizes[i - 1],
                    self.hidden_sizes[i],
                    activation_func=self.activation_funcs[i],
                    **kwargs),
            )
        # Add last layer.
        layers.append(
            DSLinearBlock(self.hidden_sizes[-1], self.num_classes, bias=self.bias),
        )

        # Create the classifier.
        self.dynamic_sparse_modules = layers[1:]
        self.classifier = nn.Sequential(*layers)

        # Initialize attr to decide whether to update coactivations during learning.
        self._track_coactivations = False  # Off by default.

    @property
    def coactivations(self):
        if self._track_coactivations:
            return [m.coactivations.t() for m in self.dynamic_sparse_modules]
        else:
            return []

    def forward(self, x):
        return self.classifier(x)

    def init_hebbian(self):
        self._track_coactivations = True
        self.apply(init_coactivation_tracking)
