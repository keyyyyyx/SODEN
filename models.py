from __future__ import absolute_import, division, print_function

import math
from collections import OrderedDict
from copy import deepcopy

import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence
from torchdiffeq import odeint_adjoint as odeint ## the ode solver with ajoint method

from metrics import NUM_INT_STEPS


class Reshape(nn.Module):
    def __init__(self, *args):
        super(Reshape, self).__init__()
        self.shape = args

    def forward(self, x):
        return x.view(self.shape)


def make_layer(layer_type, **arguments):  ## Note: ** takes specified argument names and puts them into a dictionary (https://stackoverflow.com/questions/11315010/what-do-and-before-a-variable-name-mean-in-a-function-signature)
    """Makes a layer given layer_type and arguments.

    Arguments:
      layer_type: A string indicates the type of layer. Only a limit set of
        types is supported.
      arguments: A dict of potential arguments to be used in the instantiation
        of the layer class. Among them, `input_shape` is a special one. All
        other arguments are specified by user in the model config file but
        `input_shape` is automatically reasoned in the model building process
        by stacking layers.

    Returns:
      A instantiation of the layer class.
    """
    if layer_type == "conv2d":
        if "input_shape" in arguments:
            input_shape = arguments.pop("input_shape")
            # `input_shape` should be (batch_size, in_channels, height, width)
            arguments["in_channels"] = input_shape[1]
        assert ("in_channels" in arguments and "out_channels" in arguments
                and "kernel_size" in arguments)
        layer = nn.Conv2d(**arguments)
    elif layer_type == "fc":
        sublayers = OrderedDict()
        if "input_shape" in arguments:
            input_shape = arguments.pop("input_shape")
            num_units = input_shape[1]
            # Flatten the input if needed
            if len(input_shape) > 2:
                # Calculate flat dimension
                for i in range(2, len(input_shape)):
                    num_units *= input_shape[i]
                view = Reshape(-1, num_units)  # Flatten layer
                sublayers.update({"view": view})
            arguments["in_features"] = num_units
        assert "in_features" in arguments and "out_features" in arguments
        linear = nn.Linear(**arguments)
        sublayers.update({"linear": linear})
        layer = nn.Sequential(sublayers)
    elif layer_type == "pool2d":
        if "input_shape" in arguments:
            _ = arguments.pop("input_shape")
        assert "kernel_size" in arguments
        pool_type = "max"
        if "pool_type" in arguments:
            pool_type = arguments.pop("pool_type")
        if pool_type == "max":
            layer = nn.MaxPool2d(**arguments)
        elif pool_type == "avg":
            layer = nn.AvgPool2d(**arguments)
        else:
            raise NotImplementedError(
                "Pooling type %s is not in the supported list: `max`, `avg`." %
                pool_type)
    elif layer_type == "bn1d" or layer_type == "bn2d":
        if "input_shape" in arguments:
            input_shape = arguments.pop("input_shape")
            arguments["num_features"] = input_shape[1]
        assert "num_features" in arguments
        if layer_type == "bn1d":
            layer = nn.BatchNorm1d(**arguments)
        else:
            layer = nn.BatchNorm2d(**arguments)
    elif layer_type == "relu":
        if "input_shape" in arguments:
            arguments.pop("input_shape")
        layer = nn.ReLU(**arguments)
    elif layer_type == "lrelu":
        if "input_shape" in arguments:
            arguments.pop("input_shape")
        layer = nn.LeakyReLU(**arguments)
    elif layer_type == "drop":
        if "input_shape" in arguments:
            arguments.pop("input_shape")
        layer = nn.Dropout(**arguments)
    else:
        raise NotImplementedError(
            "Layer type `%s` is not in the supported list." % layer_type)
    return layer


def make_sequential(layer_configs, input):
    """Makes sequential layers automatically.

    Arguments:
      layer_configs: An OrderedDict that contains the configurations of a
        sequence of layers. The key is the layer_name while the value is a dict
        contains hyper-parameters needed to instantiate the corresponding
        layer. The key of the inner dict is the name of the hyper-parameter and
        the value is the value of the corresponding hyper-parameter. Note that
        the key "layer_type" indicates the type of the layer.
      input: A tensor that mimics the batch input of the model. The first dim
        is the batch size. All other dims should be exactly the same as the
        real input shape in the later training.

    Returns:
      A sequence of layers organized by nn.Sequential.
    """
    layers = OrderedDict()
    for layer_name in layer_configs:
        arguments = deepcopy(layer_configs[layer_name])
        layer_type = arguments.pop("layer_type")
        input_shape = [int(j) for j in input.data.size()]
        arguments["input_shape"] = input_shape
        layers.update({layer_name: make_layer(layer_type, **arguments)})
        input = layers[layer_name](input)
    return nn.Sequential(layers)


## make a complete nn model with specified layers and sizes; take feature vector as input
def make_net(input_size, hidden_size, num_layers, output_size, dropout=0,
             batch_norm=False, act="relu", softplus=True):
    if act == "selu":
        ActFn = nn.SELU
    else:
        ActFn = nn.ReLU
    modules = [nn.Linear(input_size, hidden_size), ActFn()]   ## Applies a linear transformation to the incoming data
    if batch_norm:
        modules.append(nn.BatchNorm1d(hidden_size))
    if dropout > 0:
        modules.append(nn.Dropout(p=dropout))
    if num_layers > 1:
        for _ in range(num_layers - 1):
            modules.append(nn.Linear(hidden_size, hidden_size))
            modules.append(ActFn())
            if batch_norm:
                modules.append(nn.BatchNorm1d(hidden_size))
            if dropout > 0:
                modules.append(nn.Dropout(p=dropout))
    modules.append(nn.Linear(hidden_size, output_size))
    if softplus:  # ODE models
        modules.append(nn.Softplus())
    return nn.Sequential(*modules)

## initialize basic survival ODE function
class BaseSurvODEFunc(nn.Module):
    def __init__(self):
        super(BaseSurvODEFunc, self).__init__()
        self.nfe = 0
        self.batch_time_mode = False

    def set_batch_time_mode(self, mode=True):
        self.batch_time_mode = mode
        # `odeint` requires the output of `odefunc` to have the same size as
        # `init_cond` despite the how many steps we are going to evaluate. Set
        # `self.batch_time_mode` to `False` before calling `odeint`. However,
        # when we want to call the forward function of `odefunc` directly and
        # when we would like to evaluate multiple time steps at the same time,
        # set `self.batch_time_mode` to `True` and the output will have size
        # (len(t), size(y)).

    ## What is nfe??
    def reset_nfe(self):
        self.nfe = 0

    def forward(self, t, y):
        raise NotImplementedError("Not implemented.")


class ExpODEFunc(BaseSurvODEFunc):
    def __init__(self):
        super(ExpODEFunc, self).__init__()
        self.lamda = nn.Parameter(0.1 * torch.ones(1))

    def forward(self, t, y):
        self.nfe += 1
        if self.batch_time_mode:
            return self.lamda * torch.ones_like(t)
        else:
            return self.lamda


class MLPODEFunc(BaseSurvODEFunc):
    def __init__(self, hidden_size, num_layers, batch_norm=False):
        super(MLPODEFunc, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.batch_norm = batch_norm
        self.net = make_net(input_size=1, hidden_size=hidden_size,
                            num_layers=num_layers, output_size=1,
                            batch_norm=batch_norm)

    def forward(self, t, y):
        """
        Arguments:
          t: When self.batch_time_mode is False, t is a scalar indicating the
            time step to be evaluated. When self.batch_time_mode is True, t is
            a 1-D tensor with a single element [1.0].
          y: When self.batch_time_mode is False, y is a 1-D tensor with length
            2, where the first dim indicates Lambda_t, and the second dim
            indicates the final time step T to be evaluated. When self.batch_time_mode is True, y
            is a 2-D tensor with batch_size * 2.
        """
        self.nfe += 1
        device = next(self.parameters()).device
        T = y.index_select(-1, torch.tensor([1]).to(device)).view(-1, 1)
        inp = t.repeat(T.size()) * T
        output = self.net(inp) * T
        zeros = torch.zeros_like(T)
        output = torch.cat([output, zeros], dim=1)
        if self.batch_time_mode:
            return output
        else:
            return output.squeeze(0)

## initialize a neural network
class ContextRecMLPODEFunc(BaseSurvODEFunc):
    def __init__(self, feature_size, hidden_size, num_layers, batch_norm=False,
                 use_embed=True):
        super(ContextRecMLPODEFunc, self).__init__()
        self.feature_size = feature_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.batch_norm = batch_norm
        self.use_embed = use_embed
        if use_embed:
            self.embed = nn.Embedding(1602, self.feature_size)  ## A simple lookup table that maps index value to a weighted matrix of certain dimension
        else:
            self.embed = None
        self.net = make_net(input_size=feature_size+2, hidden_size=hidden_size,
                            num_layers=num_layers, output_size=1,
                            batch_norm=batch_norm)
    
    ## ---where does the y come from?--- passed within odeint as init_cond?
    ## forward propagetion; one step forward
    ## outputs a number from nn
    def forward(self, t, y):
        """
        Arguments:
          t: When self.batch_time_mode is False, t is a scalar indicating the
            time step to be evaluated. When self.batch_time_mode is True, t is
            a 1-D tensor with a single element [1.0].
          y: When self.batch_time_mode is False, y is a 1-D tensor with length
            2 + k, where the first dim indicates Lambda_t, the second dim
            indicates the final time step T to be evaluated, and the remaining
            k dims indicates the features. When self.batch_time_mode is True, y
            is a 2-D tensor with batch_size * (2 + k).
        """
        self.nfe += 1
        device = next(self.parameters()).device
        Lambda_t = y.index_select(-1, torch.tensor([0]).to(device)).view(-1, 1) ## retrieve Lambda_t from y, returns as a 2-D tensor of m elements
        T = y.index_select(-1, torch.tensor([1]).to(device)).view(-1, 1)  ## retrieve the final time step T from y, returns as a 2-D tensor of m elements
        x = y.index_select(-1, torch.tensor(range(2, y.size(-1))).to(device))  ## retrieve features from y, returns as a m x d matrix
        if self.use_embed:
            x = torch.mean(
                self.embed(torch.tensor(x, dtype=torch.long).to(device)),
                dim=1)
        # Rescaling trick  ## time rescaling
        # $\int_0^T f(s; x) ds = \int_0^1 T f(tT; x) dt$, where $t = s / T$
        ## cbind(Lambda_t, cbind(Lambda_t, T), x)
        inp = torch.cat(
            [Lambda_t,
             t.repeat(T.size()) * T,  ## cbind(rep(0, m), T)
             x.view(-1, self.feature_size)], dim=1)
        output = self.net(inp) * T  # f(tT; x) * T
        zeros = torch.zeros_like(
            y.index_select(-1, torch.tensor(range(1, y.size(-1))).to(device))
        )  ## Returns a tensor filled with the scalar value 0, with the same size as input
        output = torch.cat([output, zeros], dim=1)   
        if self.batch_time_mode:
            return output
        else:
            return output.squeeze(0)

## The proposed SODEN model
class NonCoxFuncModel(nn.Module):
    """NonCoxFuncModel."""

    def __init__(self, model_config, feature_size=None, use_embed=False):
        """Initializes a NonCoxFuncModel.

        Arguments:
          model_config: An OrderedDict of lists. The keys of the dict indicate
            the names of different parts of the model. Each value of the dict
            is a list indicating the configs of layers in the corresponding
            part. Each element of the list is a list [layer_type, arguments],
            where layer_type is a string and arguments is a dict.
          feature_size: Feature size.
          use_embed: Whether to use embedding layer after input.
        """
        assert feature_size is not None
        super(NonCoxFuncModel, self).__init__()
        self.model_config = model_config
        self.feature_size = feature_size
        config = model_config["ode"]["surv_ode_0"]
        self.func_type = config["func_type"]

        if self.func_type == "rec_mlp":
            self.odefunc = ContextRecMLPODEFunc(
                feature_size, config["hidden_size"], config["num_layers"],
                batch_norm=config["batch_norm"], use_embed=use_embed)     ## initialize ContextRecMLPODEFunc; ode function
        else:
            raise NotImplementedError("Function type %s is not supported."
                                      % self.func_type)

        self.set_last_eval(False)

    def set_last_eval(self, last_eval=True):
        self.last_eval = last_eval

    def forward(self, inputs):
        device = next(self.parameters()).device
        t = inputs["t"]
        init_cond = inputs["init_cond"]
        features = inputs["features"]
        init_cond = torch.cat([init_cond.view(-1, 1), t.view(-1, 1), features],
                              dim=1)  ## rearrange; equiv to c(init_cond, t, features)
        t = torch.tensor([0., 1.]).to(device)

        outputs = {}
        self.odefunc.set_batch_time_mode(False)
        outputs["Lambda"] = odeint(
            self.odefunc, init_cond, t, rtol=1e-4, atol=1e-8)[1:].squeeze()  ## returns cbind(Lambda, t, x), a m x (d+2) matrix
        if len(list(outputs["Lambda"].shape)) == 1:
            outputs["Lambda"] = outputs["Lambda"].reshape([1, list(outputs["Lambda"].shape)[0]])   ## Yueqi's edit; add to fix dimension error
        self.odefunc.set_batch_time_mode(True)
        outputs["lambda"] = self.odefunc(t[1:], outputs["Lambda"]).squeeze()  ## use ODE to find hazard function
        outputs["Lambda"] = outputs["Lambda"][:, 0] ## keep only the first column, Lambda
        if len(list(outputs["lambda"].shape)) == 1:
            outputs["lambda"] = outputs["lambda"].reshape([1, list(outputs["lambda"].shape)[0]])   ## Yueqi's edit; add to fix dimension error
        outputs["lambda"] = outputs["lambda"][:, 0] / inputs["t"] ## keep only the first column, lambda

        if not self.training:  ## ---where is self.training defined?---
           # outputs["last_eval"] = self.last_eval   ## Yueqi's edit, test last_eval
            if True: # self.last_eval and "eval_t" in inputs:
                self.odefunc.set_batch_time_mode(False)
                ones = torch.ones_like(inputs["t"])
                # Eval for time-dependent C-index
                outputs["t"] = inputs["t"]
                outputs["eval_t"] = inputs["eval_t"]
                t = inputs["eval_t"][-1] * ones
                init_cond = inputs["init_cond"]
                features = inputs["features"]
                init_cond = torch.cat(
                    [init_cond.view(-1, 1), t.view(-1, 1), features],
                    dim=1)
                t_max = inputs["eval_t"][-1]
                t = inputs["eval_t"] / t_max
                t = torch.cat([torch.zeros([1]).to(device), t], dim=0)
                outputs["cum_hazard_seqs"] = odeint(
                    self.odefunc, init_cond, t, rtol=1e-4, atol=1e-8)[1:, :, 0]   ## ---what is cum_hazard_seqs and where it is used?---

                # Eval for Brier Score
                t = inputs["t_max"] * ones
                init_cond = inputs["init_cond"]
                features = inputs["features"]
                init_cond = torch.cat(
                    [init_cond.view(-1, 1), t.view(-1, 1), features],
                    dim=1)
                t_min = inputs["t_min"]
                t_max = inputs["t_max"]
                t = torch.linspace(
                    t_min, t_max, NUM_INT_STEPS, dtype=init_cond.dtype,
                    device=device)
                t = torch.cat([torch.zeros([1]).to(device), t], dim=0)
                t = t / t_max
                outputs["survival_seqs"] = torch.exp(
                    -odeint(self.odefunc, init_cond, t, rtol=1e-4,
                            atol=1e-8)[1:, :, 0])

                for eps in [0.1, 0.2, 0.3, 0.4, 0.5]:
                    t = inputs["t_max_{}".format(eps)] * ones
                    init_cond = inputs["init_cond"]
                    features = inputs["features"]
                    init_cond = torch.cat(
                        [init_cond.view(-1, 1), t.view(-1, 1), features],
                        dim=1)
                    t_min = inputs["t_min"]
                    t_max = inputs["t_max_{}".format(eps)]
                    t = torch.linspace(
                        t_min, t_max, NUM_INT_STEPS, dtype=init_cond.dtype,
                        device=device) ## generate timesteps with given min, max, and num steps
                    t = torch.cat([torch.zeros([1]).to(device), t], dim=0)
                    t = t / t_max
                    outputs["survival_seqs_{}".format(eps)] = torch.exp(
                        -odeint(self.odefunc, init_cond, t, rtol=1e-4,
                                atol=1e-8)[1:, :, 0])
            
            ## compute Lambda at q25, q50, and q75 
            if "t_q25" in inputs:
                outputs["t"] = inputs["t"]
                self.odefunc.set_batch_time_mode(False)
                for q in ["q25", "q50", "q75"]:
                    t = inputs["t_%s" % q]
                    init_cond = inputs["init_cond"]
                    features = inputs["features"]
                    init_cond = torch.cat(
                        [init_cond.view(-1, 1), t.view(-1, 1), features],
                        dim=1)
                    t = torch.tensor([0., 1.]).to(device)
                    outputs["Lambda_%s" % q] = odeint(
                        self.odefunc, init_cond, t,
                        rtol=1e-4, atol=1e-8)[1:].squeeze()
                    if len(list(outputs["Lambda_%s" % q].shape)) == 1:
                        outputs["Lambda_%s" % q] = outputs["Lambda_%s" % q].reshape([1, list(outputs["Lambda_%s" % q].shape)[0]])   ## Yueqi's edit; add to fix dimension error
                    outputs["Lambda_%s" % q] = outputs["Lambda_%s" % q][:, 0]

        return outputs

## SODEN-PH and SODEN-Cox respectively correspond to cox_mlp_mlp and cox_mlp_exp
class CoxFuncModel(nn.Module):
    """CoxFuncModel."""

    def __init__(self, model_config, feature_size=None, use_embed=False):
        """Initializes a CoxFuncModel.

        Arguments:
          model_config: An OrderedDict of lists. The keys of the dict indicate
            the names of different parts of the model. Each value of the dict
            is a list indicating the configs of layers in the corresponding
            part. Each element of the list is a list [layer_type, arguments],
            where layer_type is a string and arguments is a dict.
          feature_size: Feature size.
          use_embed: Whether to use embedding layer after input.
        """
        super(CoxFuncModel, self).__init__()
        self.model_config = model_config
        self.feature_size = feature_size
        config = model_config["ode"]["surv_ode_0"]
        self.func_type = config["func_type"]
        self.has_feature = config["has_feature"]
        self.use_embed = use_embed
        if self.has_feature:
            if self.func_type == "cox_mlp_exp":    ## SODEN-Cox model
                beta_init = torch.randn(feature_size) / math.sqrt(feature_size)
                beta_init = beta_init.view(-1, 1)
                self.beta = nn.Parameter(beta_init)
            elif self.func_type == "cox_mlp_mlp":    ## SODEN-PH model
                hidden_size = config["hidden_size"]
                num_layers = config["num_layers"]
                self.x_net = make_net(
                    input_size=feature_size, hidden_size=hidden_size,
                    num_layers=num_layers, output_size=1,
                    batch_norm=config["batch_norm"])
            else:
                raise NotImplementedError(
                    "Type %s not supported." % self.func_type)
            if use_embed:
                self.embed = nn.Embedding(1602, self.feature_size)
            else:
                self.embed = None

        if self.func_type == "exponential":
            self.odefunc = ExpODEFunc()
        elif (self.func_type == "cox_mlp_exp" or
              self.func_type == "cox_mlp_mlp"):
            self.odefunc = MLPODEFunc(
                config["hidden_size"], config["num_layers"],
                batch_norm=config["batch_norm"])
        else:
            raise NotImplementedError("Function type %s is not supported."
                                      % self.func_type)

        self.set_last_eval(False)

    def set_last_eval(self, last_eval=True):
        self.last_eval = last_eval

    def forward(self, inputs):
        device = next(self.parameters()).device
        t = inputs["t"]
        init_cond = inputs["init_cond"]
        init_cond = torch.cat([init_cond.view(-1, 1), t.view(-1, 1)], dim=1)
        if self.has_feature:
            assert "features" in inputs
            x = inputs["features"]
            if self.use_embed:
                x = torch.mean(
                    self.embed(torch.tensor(x, dtype=torch.long).to(device)),
                    dim=1)
            if self.func_type == "cox_mlp_exp":
                prod = torch.mm(x, self.beta).squeeze()
            elif self.func_type == "cox_mlp_mlp":
                prod = self.x_net(x).squeeze()
        t = torch.tensor([0., 1.]).to(device)

        outputs = {}
        self.odefunc.set_batch_time_mode(False)
        outputs["Lambda"] = odeint(self.odefunc, init_cond, t, rtol=1e-4, atol=1e-8)[1:].squeeze()
        self.odefunc.set_batch_time_mode(True)
        outputs["lambda"] = self.odefunc(t[1:], outputs["Lambda"]).squeeze()
        outputs["Lambda"] = outputs["Lambda"][:, 0]
        outputs["lambda"] = outputs["lambda"][:, 0] / inputs["t"]
        if self.has_feature:
            if self.func_type == "cox_mlp_mlp":
                prod_exp = prod
                prod = torch.log(prod.clamp(min=1e-8))
            else:
                prod_exp = torch.exp(prod.clamp(max=10))
            outputs["Lambda"] = outputs["Lambda"] * prod_exp
            outputs["log_lambda"] = torch.log(
                outputs["lambda"].clamp(min=1e-8)) + prod
            outputs["lambda"] = outputs["lambda"] * prod_exp

        if not self.training and self.has_feature:
            outputs["prod"] = prod
            outputs["t"] = inputs["t"]

            if self.last_eval and "eval_t" in inputs:
                self.odefunc.set_batch_time_mode(False)
                ones = torch.ones_like(inputs["t"])

                # # Eval for time-dependent C-index (for sanity check)
                # outputs["t"] = inputs["t"]
                # outputs["eval_t"] = inputs["eval_t"]
                # t = inputs["eval_t"][-1] * ones
                # init_cond = inputs["init_cond"]
                # init_cond = torch.cat(
                #     [init_cond.view(-1, 1), t.view(-1, 1)], dim=1)
                # t_max = inputs["eval_t"][-1]
                # t = inputs["eval_t"] / t_max
                # t = torch.cat([torch.zeros([1]).to(device), t], dim=0)
                # outputs["cum_hazard_seqs"] = (
                #     odeint(self.odefunc, init_cond, t, rtol=1e-4,
                #            atol=1e-8)[1:, :, 0] * prod_exp.view(1, -1))

                # Eval for Brier Score
                t = inputs["t_max"] * ones
                init_cond = inputs["init_cond"]
                init_cond = torch.cat(
                    [init_cond.view(-1, 1), t.view(-1, 1)], dim=1)
                t_min = inputs["t_min"]
                t_max = inputs["t_max"]
                t = torch.linspace(
                    t_min, t_max, NUM_INT_STEPS, dtype=init_cond.dtype,
                    device=device)
                t = torch.cat([torch.zeros([1]).to(device), t], dim=0)
                t = t / t_max
                outputs["survival_seqs"] = torch.exp(
                    -odeint(self.odefunc, init_cond, t, rtol=1e-4,
                            atol=1e-8)[1:, :, 0] * prod_exp.view(1, -1))

                for eps in [0.1, 0.2, 0.3, 0.4, 0.5]:
                    t = inputs["t_max_{}".format(eps)] * ones
                    init_cond = inputs["init_cond"]
                    init_cond = torch.cat(
                        [init_cond.view(-1, 1), t.view(-1, 1)], dim=1)
                    t_min = inputs["t_min"]
                    t_max = inputs["t_max_{}".format(eps)]
                    t = torch.linspace(
                        t_min, t_max, NUM_INT_STEPS, dtype=init_cond.dtype,
                        device=device)
                    t = torch.cat([torch.zeros([1]).to(device), t], dim=0)
                    t = t / t_max
                    outputs["survival_seqs_{}".format(eps)] = torch.exp(
                        -odeint(self.odefunc, init_cond, t, rtol=1e-4,
                                atol=1e-8)[1:, :, 0] * prod_exp.view(1, -1))

        return outputs


class SODENModel(nn.Module):
    """SODENModel."""

    def __init__(self, model_config, feature_size=None, use_embed=False):
        """Initializes a SODENModel.

        Arguments:
          model_config: An OrderedDict of lists. The keys of the dict indicate
            the names of different parts of the model. Each value of the dict
            is a list indicating the configs of layers in the corresponding
            part. Each element of the list is a list [layer_type, arguments],
            where layer_type is a string and arguments is a dict.
          feature_size: Feature size.
          use_embed: Whether to use embedding layer after input.
        """
        super(SODENModel, self).__init__()
        ## rnn: recurrent neural network
        if "rnn" in model_config:
            self.rnn_config = model_config["rnn"]["rnn_0"]
            if self.rnn_config["rnn_type"] == "LSTM":
                RNNModel = nn.LSTM  ## Applies a multi-layer long short-term memory (LSTM) RNN to an input sequence.
            elif self.rnn_config["rnn_type"] == "GRU": 
                RNNModel = nn.GRU   ## Applies a multi-layer gated recurrent unit (GRU) RNN to an input sequence.
            else:
                raise NotImplementedError(
                    "Unsupported RNN type: %s." % self.rnn_type)
            seq_feat_size = feature_size["seq_feat"]
            self.rnn = RNNModel(input_size=seq_feat_size,
                                hidden_size=self.rnn_config["hidden_size"],
                                num_layers=self.rnn_config["num_layers"],
                                batch_first=True)

            feature_size = self.rnn_config["hidden_size"] + feature_size["fix_feat"]
        else:
            self.rnn = None
        config = model_config["ode"]["surv_ode_0"]
        if config["layer_type"] == "surv_ode":
            if config["func_type"] in ["rec_mlp"]:  ## the proposed SODEN model
                self.model = NonCoxFuncModel(model_config, feature_size, use_embed)
            elif config["func_type"] in ["cox_mlp_exp", "cox_mlp_mlp"]:       ## SODEN-PH and SODEN-Cox respectively correspond to cox_mlp_mlp and cox_mlp_exp
                self.model = CoxFuncModel(
                    model_config, feature_size, use_embed)
            else:
                raise NotImplementedError("func_type %s not supported." % config["func_type"])
        else:
            raise NotImplementedError("Model %s not supported." % config["layer_type"])

    def set_last_eval(self, last_eval=True):
        if hasattr(self.model, "set_last_eval"):
            self.model.set_last_eval(last_eval)

    def forward(self, inputs):
        if self.rnn is not None:
            seq_feat = inputs.pop("seq_feat")
            seq_feat_length = inputs.pop("seq_feat_length")
            device = seq_feat.device
            if self.rnn_config["rnn_type"] == "LSTM":
                h0 = torch.zeros(
                    self.rnn_config["num_layers"], seq_feat.size(0),
                    self.rnn_config["hidden_size"]).to(device)
                c0 = torch.zeros(
                    self.rnn_config["num_layers"], seq_feat.size(0),
                    self.rnn_config["hidden_size"]).to(device)
                h_init = (h0, c0)
            else:
                h_init = torch.zeros(
                    self.rnn_config["num_layers"], seq_feat.size(0),
                    self.rnn_config["hidden_size"]).to(device)

            seq_feat = pack_padded_sequence(seq_feat, seq_feat_length,
                                            batch_first=True)
            _, h_final = self.rnn(seq_feat, h_init)
            if self.rnn_config["rnn_type"] == "LSTM":
                h_final = h_final[0]  # h_final was (h_n, c_n)
            h_final = h_final[-1]  # shape was (num_layers, batch, hidden_size)

            fix_feat = inputs.pop("fix_feat")
            inputs["features"] = torch.cat([fix_feat, h_final], dim=1)

        return self.model(inputs) ## call either NonCoxFuncModel or CoxFuncModel with specified inputs
