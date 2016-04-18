from blocks.bricks import (
    Initializable, Linear, Logistic, Random)
from blocks.bricks.base import application
from blocks.bricks.parallel import Fork
from blocks.bricks.recurrent import GatedRecurrent
from blocks.bricks.sequence_generators import AbstractEmitter
from blocks.utils import shared_floatx_zeros

import numpy

import theano
from theano import tensor, function

from utils import (
    mean_f0, mean_spectrum, std_f0, std_spectrum,
    spectrum_lower_limit, spectrum_upper_limit, min_voiced_lower_limit)

floatX = theano.config.floatX


def _slice_last(mat, start, end):
    # Slice across the last dimension
    return mat[[slice(None)] * (mat.ndim - 1) + [slice(start, end)]]


# https://gist.github.com/benanne/2300591
def one_hot(t, r=None):
    """Compute one hot encoding.

    given a tensor t of dimension d with integer values from range(r), return a
    new tensor of dimension d + 1 with values 0/1, where the last dimension
    gives a one-hot representation of the values in t.
    if r is not given, r is set to max(t) + 1

    """
    if r is None:
        r = tensor.max(t) + 1

    ranges = tensor.shape_padleft(tensor.arange(r), t.ndim)
    return tensor.eq(ranges, tensor.shape_padright(t, 1))


def logsumexp(x, axis=None):
    x_max = tensor.max(x, axis=axis, keepdims=True)
    z = tensor.log(
        tensor.sum(tensor.exp(x - x_max), axis=axis, keepdims=True)) + x_max
    return z.sum(axis=axis)


def predict(probs, axis=-1):
    return tensor.argmax(probs, axis=axis)


def cost_gmm(y, mu, sig, weight):
    """Gaussian mixture model negative log-likelihood.

    Computes the cost.

    """
    n_dim = y.ndim
    shape_y = y.shape

    k = weight.shape[-1]
    dim = mu.shape[-2]

    y = y.reshape((-1, shape_y[-1]))
    y = tensor.shape_padright(y)

    mu = mu.reshape((-1, dim, k))
    sig = sig.reshape((-1, dim, k))
    weight = weight.reshape((-1, k))

    inner = -0.5 * tensor.sum(
        tensor.sqr(y - mu) / sig**2 +
        2 * tensor.log(sig) + tensor.log(2 * numpy.pi), axis=-2)

    nll = -logsumexp(tensor.log(weight) + inner, axis=-1)

    return nll.reshape(shape_y[:-1], ndim=n_dim - 1)


def sample_gmm(mu, sigma, weight, theano_rng):

    k = weight.shape[-1]
    dim = mu.shape[-2]

    shape_result = weight.shape
    shape_result = tensor.set_subtensor(shape_result[-1], dim)
    ndim_result = weight.ndim

    mu = mu.reshape((-1, dim, k))
    sigma = sigma.reshape((-1, dim, k))
    weight = weight.reshape((-1, k))

    sample_weight = theano_rng.multinomial(pvals=weight, dtype=weight.dtype)
    idx = predict(sample_weight, axis=-1)

    mu = mu[tensor.arange(mu.shape[0]), :, idx]
    sigma = sigma[tensor.arange(sigma.shape[0]), :, idx]

    epsilon = theano_rng.normal(
        size=mu.shape, avg=0., std=1., dtype=mu.dtype)

    result = mu + sigma * epsilon

    return result.reshape(shape_result, ndim=ndim_result)


class GMMEmitter(Initializable, AbstractEmitter, Random):
    """A GMM emitter for the case of real outputs.

    NLL cost and gmm sampling.

    """

    def __init__(self, input_dim, dim, k, sampl_bias=0., const=1e-5, **kwargs):
        super(GMMEmitter, self).__init__(**kwargs)

        self.epsilon = const
        self.k = k
        self.dim = dim
        self.input_dim = input_dim
        self.sampling_bias = sampl_bias
        self.full_dim = 2 * self.dim * self.k + self.k

        self.linear = Linear(
            input_dim=self.input_dim,
            output_dim=self.full_dim,
            name='gmm_mlp_linear')

        self.children = [self.linear]

    @application
    def components(self, inputs):
        state = self.linear.apply(inputs)
        k = self.k
        dim = self.dim

        weights_shape = inputs.shape
        weights_shape = tensor.set_subtensor(weights_shape[-1], k)

        results_shape = tensor.shape_padright(inputs).shape
        results_shape = tensor.set_subtensor(results_shape[-2], dim)
        results_shape = tensor.set_subtensor(results_shape[-1], k)
        inputs_ndim = inputs.ndim

        mu = _slice_last(state, 0, dim * k).reshape(
            results_shape, ndim=inputs_ndim + 1)
        sigma = _slice_last(state, dim * k, 2 * dim * k).reshape(
            results_shape, ndim=inputs_ndim + 1)
        weight = _slice_last(state, 2 * dim * k, 2 * dim * k + k).reshape(
            (-1, k))

        sigma = tensor.exp(sigma - self.sampling_bias) + self.epsilon
        weight = tensor.nnet.softmax(weight * (1. + self.sampling_bias)) + \
            self.epsilon

        weight = weight.reshape(weights_shape, ndim=inputs_ndim)

        return mu, sigma, weight

    @application
    def cost(self, readouts, outputs):
        mu, sigma, weight = self.components(readouts)
        return cost_gmm(outputs, mu, sigma, weight)

    @application
    def emit(self, readouts):
        mu, sigma, weight = self.components(readouts)
        return sample_gmm(mu, sigma, weight, self.theano_rng)

    @application
    def initial_outputs(self, batch_size):
        return tensor.zeros((batch_size, self.dim), dtype=floatX)


class F0Emitter(AbstractEmitter, Initializable, Random):
    def __init__(self, input_dim, k_f0=5, sampl_bias=0., const=1e-5, **kwargs):
        super(F0Emitter, self).__init__(**kwargs)

        self.sampl_bias = sampl_bias

        self.f0_gmm = GMMEmitter(
            input_dim=input_dim,
            dim=1,
            k=k_f0,
            sampl_bias=sampl_bias,
            const=const,
            name="f0_emitter")

        self.binary = Linear(
            input_dim=input_dim,
            output_dim=1,
            name='f0_emitter_binary')

        self.logistic = Logistic()

        self.children = [self.binary, self.f0_gmm, self.logistic]

    @application
    def emit(self, readouts):
        binary = self.binary.apply(readouts)
        binary = self.logistic.apply(binary * (1. + self.sampl_bias))
        un = self.theano_rng.uniform(size=binary.shape)
        binary_sample = tensor.cast(un < binary, floatX)
        f0_sample = self.f0_gmm.emit(readouts)

        # Clip to max value in dataset: 300
        f0_sample = tensor.minimum(
            f0_sample, (300. - mean_f0) / std_f0)

        f0_sample = tensor.maximum(
            f0_sample, (min_voiced_lower_limit - mean_f0) / std_f0)

        f0_sample = f0_sample * binary_sample

        return f0_sample, binary_sample

    @application
    def cost(self, readouts, f0, voiced):
        binary = self.binary.apply(readouts)
        binary = self.logistic.apply(binary)
        binary = binary.flatten(ndim=binary.ndim - 1)
        c_b = tensor.xlogx.xlogy0(voiced, binary) + \
            tensor.xlogx.xlogy0(1 - voiced, 1 - binary)
        gmm_cost = self.f0_gmm.cost(readouts, tensor.shape_padright(f0))
        return gmm_cost * voiced - c_b

    @application
    def initial_outputs(self, batch_size):
        return tensor.zeros((batch_size, self.frame_size), dtype=floatX)

    def get_dim(self, name):
        if name == 'outputs':
            return self.frame_size
        return super(F0Emitter, self).get_dim(name)


class Parrot(Initializable):
    def __init__(
            self,
            num_freq=257,
            k=20,
            k_f0=5,
            rnn1_h_dim=400,
            rnn2_h_dim=100,
            att_size=10,
            num_letters=28,
            readouts_dim=200,
            sampling_bias=0.,
            **kwargs):

        super(Parrot, self).__init__(**kwargs)

        self.num_freq = num_freq
        self.k = k
        self.rnn1_h_dim = rnn1_h_dim
        self.rnn2_h_dim = rnn2_h_dim
        self.att_size = att_size
        self.num_letters = num_letters
        self.sampling_bias = sampling_bias
        self.readouts_dim = readouts_dim

        self.rnn1_cell1 = GatedRecurrent(dim=rnn1_h_dim, name='rnn1_cell1')
        self.rnn1_cell2 = GatedRecurrent(dim=rnn1_h_dim, name='rnn1_cell2')
        self.rnn1_cell3 = GatedRecurrent(dim=rnn1_h_dim, name='rnn1_cell3')

        self.inp_to_h1 = Fork(
            output_names=['rnn1_cell1_inputs', 'rnn1_cell1_gates'],
            input_dim=num_freq + 2,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='inp_to_h1')

        self.inp_to_h2 = Fork(
            output_names=['rnn1_cell2_inputs', 'rnn1_cell2_gates'],
            input_dim=num_freq + 2,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='inp_to_h2')

        self.inp_to_h3 = Fork(
            output_names=['rnn1_cell3_inputs', 'rnn1_cell3_gates'],
            input_dim=num_freq + 2,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='inp_to_h3')

        self.h1_to_h2 = Fork(
            output_names=['rnn1_cell2_inputs', 'rnn1_cell2_gates'],
            input_dim=rnn1_h_dim,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='h1_to_h2')

        self.h1_to_h3 = Fork(
            output_names=['rnn1_cell3_inputs', 'rnn1_cell3_gates'],
            input_dim=rnn1_h_dim,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='h1_to_h3')

        self.h2_to_h3 = Fork(
            output_names=['rnn1_cell3_inputs', 'rnn1_cell3_gates'],
            input_dim=rnn1_h_dim,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='h2_to_h3')

        self.h1_to_readout = Linear(
            input_dim=rnn1_h_dim,
            output_dim=readouts_dim,
            name='h1_to_readout')

        self.h2_to_readout = Linear(
            input_dim=rnn1_h_dim,
            output_dim=readouts_dim,
            name='h2_to_readout')

        self.h3_to_readout = Linear(
            input_dim=rnn1_h_dim,
            output_dim=readouts_dim,
            name='h3_to_readout')

        self.h1_to_att = Fork(
            output_names=['alpha', 'beta', 'kappa'],
            input_dim=rnn1_h_dim,
            output_dims=[att_size] * 3,
            name='h1_to_att')

        self.att_to_h1 = Fork(
            output_names=['rnn1_cell1_inputs', 'rnn1_cell1_gates'],
            input_dim=num_letters,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='att_to_h1')

        self.att_to_h2 = Fork(
            output_names=['rnn1_cell2_inputs', 'rnn1_cell2_gates'],
            input_dim=num_letters,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='att_to_h2')

        self.att_to_h3 = Fork(
            output_names=['rnn1_cell3_inputs', 'rnn1_cell3_gates'],
            input_dim=num_letters,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='att_to_h3')

        self.rnn2_cell1 = GatedRecurrent(dim=rnn2_h_dim, name='rnn2_cell1')

        self.readouts_to_rnn2 = Fork(
            output_names=['rnn2_cell1_inputs', 'rnn2_cell1_gates'],
            input_dim=readouts_dim,
            output_dims=[rnn2_h_dim, 2 * rnn2_h_dim],
            name='readouts_to_rnn2')

        self.spectrum_to_rnn2 = Fork(
            output_names=['rnn2_cell1_inputs', 'rnn2_cell1_gates'],
            input_dim=1,
            output_dims=[rnn2_h_dim, 2 * rnn2_h_dim],
            name='spectrum_to_rnn2')

        self.f0_to_rnn2 = Fork(
            output_names=['rnn2_cell1_inputs', 'rnn2_cell1_gates'],
            input_dim=2,  # f0 and voiced
            output_dims=[rnn2_h_dim, 2 * rnn2_h_dim],
            name='f0_to_rnn2')

        self.data_to_rnn2 = Fork(
            output_names=['rnn2_cell1_inputs', 'rnn2_cell1_gates'],
            input_dim=num_freq + 2,
            output_dims=[rnn2_h_dim, 2 * rnn2_h_dim],
            name='data_to_rnn2')

        self.f0_emitter = F0Emitter(
            input_dim=readouts_dim,
            k_f0=k_f0,
            sampl_bias=sampling_bias)

        self.spectrum_emitter = GMMEmitter(
            input_dim=rnn2_h_dim,
            dim=1,
            k=k,
            sampl_bias=sampling_bias)

        self.children = [
            self.rnn1_cell1, self.rnn1_cell2, self.rnn1_cell3,
            self.inp_to_h1, self.inp_to_h2, self.inp_to_h3,
            self.h1_to_h2, self.h1_to_h3, self.h2_to_h3,
            self.h1_to_readout, self.h2_to_readout, self.h3_to_readout,
            self.h1_to_att, self.att_to_h1, self.att_to_h2, self.att_to_h3,
            self.readouts_to_rnn2, self.spectrum_to_rnn2, self.f0_to_rnn2,
            self.rnn2_cell1, self.f0_emitter, self.spectrum_emitter,
            self.data_to_rnn2]

    def symbolic_input_variables(self):
        f0 = tensor.matrix('f0')
        voiced = tensor.matrix('voiced')
        start_flag = tensor.scalar('start_flag')
        spectrum = tensor.tensor3('spectrum')
        transcripts = tensor.imatrix('transcripts')
        transcripts_mask = tensor.matrix('transcripts_mask')
        f0_mask = tensor.matrix('f0_mask')

        return f0, f0_mask, voiced, spectrum, \
            transcripts, transcripts_mask, start_flag

    def initial_states(self, batch_size):
        initial_h1 = shared_floatx_zeros((batch_size, self.rnn1_h_dim))
        initial_h2 = shared_floatx_zeros((batch_size, self.rnn1_h_dim))
        initial_h3 = shared_floatx_zeros((batch_size, self.rnn1_h_dim))
        initial_kappa = shared_floatx_zeros((batch_size, self.att_size))
        initial_w = shared_floatx_zeros((batch_size, self.num_letters))

        return initial_h1, initial_h2, initial_h3, initial_kappa, initial_w

    def symbolic_initial_states(self):
        initial_h1 = tensor.matrix('initial_h1')
        initial_h2 = tensor.matrix('initial_h1')
        initial_h3 = tensor.matrix('initial_h1')
        initial_kappa = tensor.matrix('initial_h1')
        initial_w = tensor.matrix('initial_h1')

        return initial_h1, initial_h2, initial_h3, initial_kappa, initial_w

    def numpy_initial_states(self, batch_size):
        initial_h1 = numpy.zeros((batch_size, self.rnn1_h_dim))
        initial_h2 = numpy.zeros((batch_size, self.rnn1_h_dim))
        initial_h3 = numpy.zeros((batch_size, self.rnn1_h_dim))
        initial_kappa = numpy.zeros((batch_size, self.att_size))
        initial_w = numpy.zeros((batch_size, self.num_letters))

        return initial_h1, initial_h2, initial_h3, initial_kappa, initial_w

    def compute_cost(
            self, f0, f0_mask, voiced, spectrum, transcripts, transcripts_mask,
            start_flag, batch_size, seq_length):

        f0_pr = tensor.shape_padright(f0)
        voiced_pr = tensor.shape_padright(voiced)

        data = tensor.concatenate([spectrum, f0_pr, voiced_pr], 2)

        x = data[:-1]
        # target = data[1:]
        mask = f0_mask[1:]

        target_f0 = f0[1:]
        target_voiced = voiced[1:]
        target_spectrum = spectrum[1:]

        xinp_h1, xgat_h1 = self.inp_to_h1.apply(x)
        xinp_h2, xgat_h2 = self.inp_to_h2.apply(x)
        xinp_h3, xgat_h3 = self.inp_to_h3.apply(x)
        transcripts_oh = one_hot(transcripts, self.num_letters) * \
            tensor.shape_padright(transcripts_mask)

        initial_h1, initial_h2, initial_h3, initial_kappa, initial_w = \
            self.initial_states(batch_size)

        # size of transcripts: = transcripts.shape[1]
        u = tensor.shape_padleft(
            tensor.arange(transcripts.shape[1], dtype=floatX), 2)

        def step(xinp_h1_t, xgat_h1_t, xinp_h2_t, xgat_h2_t, xinp_h3_t,
                 xgat_h3_t, h1_tm1, h2_tm1, h3_tm1, k_tm1, w_tm1, ctx):

            attinp_h1, attgat_h1 = self.att_to_h1.apply(w_tm1)

            h1_t = self.rnn1_cell1.apply(
                xinp_h1_t + attinp_h1,
                xgat_h1_t + attgat_h1, h1_tm1, iterate=False)
            h1inp_h2, h1gat_h2 = self.h1_to_h2.apply(h1_t)
            h1inp_h3, h1gat_h3 = self.h1_to_h3.apply(h1_t)

            a_t, b_t, k_t = self.h1_to_att.apply(h1_t)

            a_t = tensor.exp(a_t)
            b_t = tensor.exp(b_t)
            k_t = k_tm1 + tensor.exp(k_t)

            a_t = tensor.shape_padright(a_t)
            b_t = tensor.shape_padright(b_t)
            k_t_ = tensor.shape_padright(k_t)

            # batch size X att size X len transcripts
            phi_t = tensor.sum(a_t * tensor.exp(-b_t * (k_t_ - u)**2), axis=1)

            # batch size X len transcripts X num letters
            ss6 = tensor.shape_padright(phi_t) * ctx
            w_t = ss6.sum(axis=1)

            # batch size X num letters
            attinp_h2, attgat_h2 = self.att_to_h2.apply(w_t)
            attinp_h3, attgat_h3 = self.att_to_h3.apply(w_t)

            h2_t = self.rnn1_cell2.apply(
                xinp_h2_t + h1inp_h2 + attinp_h2,
                xgat_h2_t + h1gat_h2 + attgat_h2, h2_tm1,
                iterate=False)

            h2inp_h3, h2gat_h3 = self.h2_to_h3.apply(h2_t)

            h3_t = self.rnn1_cell3.apply(
                xinp_h3_t + h1inp_h3 + h2inp_h3 + attinp_h3,
                xgat_h3_t + h1gat_h3 + h2gat_h3 + attgat_h3, h3_tm1,
                iterate=False)

            return h1_t, h2_t, h3_t, k_t, w_t

        (h1, h2, h3, kappa, w), scan_updates = theano.scan(
            fn=step,
            sequences=[xinp_h1, xgat_h1, xinp_h2, xgat_h2, xinp_h3, xgat_h3],
            non_sequences=[transcripts_oh],
            outputs_info=[initial_h1, initial_h2, initial_h3,
                          initial_kappa, initial_w])

        readouts = self.h1_to_readout.apply(h1) + \
            self.h2_to_readout.apply(h2) + \
            self.h3_to_readout.apply(h3)

        cost_f0 = self.f0_emitter.cost(readouts, target_f0, target_voiced)

        initial_rnn2 = tensor.zeros((seq_length * batch_size, self.rnn2_h_dim))

        target_spectrum = spectrum[1:].reshape(
            (batch_size * seq_length, self.num_freq))
        target_spectrum = tensor.shape_padright(target_spectrum.swapaxes(0, 1))

        # Normal input of the recurrent network
        # Adding a column of zeros to start the rnn2
        input_spectrum = tensor.concatenate([
            tensor.zeros((1, batch_size * seq_length, 1)),
            target_spectrum[:-1]], axis=0)
        sp_inputs, sp_gates = self.spectrum_to_rnn2.apply(input_spectrum)

        # Input coming from the timewise rnn
        readout_inputs, readouts_gates = self.readouts_to_rnn2.apply(
            readouts.reshape((batch_size * seq_length, self.readouts_dim)))

        # Input coming from the f0 + voiced (this timestep)
        f0v_cond = tensor.concatenate(
            [f0_pr, voiced_pr], 2)[1:].reshape((batch_size * seq_length, 2))
        f0v_inputs, f0v_gates = self.f0_to_rnn2.apply(f0v_cond)

        # Input coming from the last timestep
        data_inputs, data_gates = self.data_to_rnn2.apply(
            x.reshape((batch_size * seq_length, self.num_freq + 2)))

        rnn2_inputs = sp_inputs + \
            tensor.shape_padleft(readout_inputs) + \
            tensor.shape_padleft(f0v_inputs) + \
            tensor.shape_padleft(data_inputs)

        rnn2_gates = sp_gates + \
            tensor.shape_padleft(readouts_gates) + \
            tensor.shape_padleft(f0v_gates) + \
            tensor.shape_padleft(data_gates)

        def step_rnn2(xinp_h1_t, xgat_h1_t, h1_tm1):
            h1_t = self.rnn2_cell1.apply(
                xinp_h1_t, xgat_h1_t, h1_tm1, iterate=False)
            return h1_t

        h_rnn2, scan2_updates = theano.scan(
            fn=step_rnn2,
            sequences=[rnn2_inputs, rnn2_gates],
            non_sequences=[],
            outputs_info=[initial_rnn2])

        cost_gmm = self.spectrum_emitter.cost(
            h_rnn2, target_spectrum).sum(axis=0).reshape(
            (seq_length, batch_size))

        # cost = self.emitter.cost(readouts, target)
        cost = cost_f0 + cost_gmm
        cost = (cost * mask).sum() / (mask.sum() + 1e-5) + 0. * start_flag
        cost.name = 'nll'

        updates = []
        updates.append((
            initial_h1,
            tensor.switch(start_flag, 0. * initial_h1, h1[-1])))
        updates.append((
            initial_h2,
            tensor.switch(start_flag, 0. * initial_h2, h2[-1])))
        updates.append((
            initial_h3,
            tensor.switch(start_flag, 0. * initial_h3, h3[-1])))
        updates.append((
            initial_kappa,
            tensor.switch(start_flag, 0. * initial_kappa, kappa[-1])))
        updates.append((
            initial_w,
            tensor.switch(start_flag, 0. * initial_w, w[-1])))

        return cost, scan_updates + updates

    def sample_one_step(self, num_samples):

        f0, f0_mask, voiced, spectrum, transcripts, \
            transcripts_mask, start_flag = \
            self.symbolic_input_variables()

        initial_h1, initial_h2, initial_h3, initial_kappa, initial_w = \
            self.initial_states(num_samples)

        initial_x = tensor.matrix('x')

        transcripts_oh = one_hot(transcripts, self.num_letters) * \
            tensor.shape_padright(transcripts_mask)

        u = tensor.shape_padleft(
            tensor.arange(transcripts.shape[1], dtype=floatX), 2)

        x_tm1 = initial_x
        h1_tm1 = initial_h1
        h2_tm1 = initial_h2
        h3_tm1 = initial_h3
        k_tm1 = initial_kappa
        w_tm1 = initial_w
        ctx = transcripts_oh

        xinp_h1_t, xgat_h1_t = self.inp_to_h1.apply(x_tm1)
        xinp_h2_t, xgat_h2_t = self.inp_to_h2.apply(x_tm1)
        xinp_h3_t, xgat_h3_t = self.inp_to_h3.apply(x_tm1)

        attinp_h1, attgat_h1 = self.att_to_h1.apply(w_tm1)

        h1_t = self.rnn1_cell1.apply(
            xinp_h1_t + attinp_h1,
            xgat_h1_t + attgat_h1, h1_tm1, iterate=False)
        h1inp_h2, h1gat_h2 = self.h1_to_h2.apply(h1_t)
        h1inp_h3, h1gat_h3 = self.h1_to_h3.apply(h1_t)

        a_t, b_t, k_t = self.h1_to_att.apply(h1_t)

        a_t = tensor.exp(a_t)
        b_t = tensor.exp(b_t)
        k_t = k_tm1 + tensor.exp(k_t)

        a_t = tensor.shape_padright(a_t)
        b_t = tensor.shape_padright(b_t)
        k_t_ = tensor.shape_padright(k_t)

        # batch size X att size X len transcripts
        phi_t = tensor.sum(a_t * tensor.exp(-b_t * (k_t_ - u)**2), axis=1)

        # batch size X len transcripts X num letters
        w_t = (tensor.shape_padright(phi_t) * ctx).sum(axis=1)

        # batch size X num letters
        attinp_h2, attgat_h2 = self.att_to_h2.apply(w_t)
        attinp_h3, attgat_h3 = self.att_to_h3.apply(w_t)

        h2_t = self.rnn1_cell2.apply(
            xinp_h2_t + h1inp_h2 + attinp_h2,
            xgat_h2_t + h1gat_h2 + attgat_h2, h2_tm1,
            iterate=False)

        h2inp_h3, h2gat_h3 = self.h2_to_h3.apply(h2_t)

        h3_t = self.rnn1_cell3.apply(
            xinp_h3_t + h1inp_h3 + h2inp_h3 + attinp_h3,
            xgat_h3_t + h1gat_h3 + h2gat_h3 + attgat_h3, h3_tm1,
            iterate=False)

        readout_t = self.h1_to_readout.apply(h1_t) + \
            self.h2_to_readout.apply(h2_t) + \
            self.h3_to_readout.apply(h3_t)

        f0_t, voiced_t = self.f0_emitter.emit(readout_t)

        initial_rnn2 = tensor.zeros((num_samples, self.rnn2_h_dim))
        rnn2_xtm1 = shared_floatx_zeros((num_samples, 1))

        readout_inputs, readouts_gates = self.readouts_to_rnn2.apply(
            readout_t.reshape((num_samples * 1, self.readouts_dim)))

        f0_pr = tensor.shape_padright(f0_t)
        voiced_pr = tensor.shape_padright(voiced_t)

        f0v_cond = tensor.concatenate(
            [f0_pr, voiced_pr], 2).reshape((num_samples * 1, 2))
        f0v_inputs, f0v_gates = self.f0_to_rnn2.apply(f0v_cond)

        data_inputs, data_gates = self.data_to_rnn2.apply(
            x_tm1.reshape((num_samples * 1, self.num_freq + 2)))

        context_inputs = readout_inputs + f0v_inputs + data_inputs
        context_gates = readouts_gates + f0v_gates + data_gates

        def sample_step(x_tm1, h1_tm1, ctx_inp, ctx_gat):
            sp_inputs_t, sp_gates_t = self.spectrum_to_rnn2.apply(x_tm1)

            h1_t = self.rnn2_cell1.apply(
                sp_inputs_t + ctx_inp,
                sp_gates_t + ctx_gat,
                h1_tm1, iterate=False)

            x_t = self.spectrum_emitter.emit(h1_t)
            mu_t, sigma_t, pi_t = self.spectrum_emitter.components(h1_t)

            return x_t, h1_t, pi_t

        (sample_spectrum, h1, pi), updates = theano.scan(
            fn=sample_step,
            n_steps=self.num_freq,
            sequences=[],
            non_sequences=[context_inputs, context_gates],
            outputs_info=[rnn2_xtm1, initial_rnn2, None])

        sample_spectrum = sample_spectrum.reshape(
            (self.num_freq, num_samples)).swapaxes(0, 1)
        new_x = tensor.concatenate([sample_spectrum, f0_t, voiced_t], 1)

        extra_updates = []

        extra_updates.append((initial_h1, h1_t))
        extra_updates.append((initial_h2, h2_t))
        extra_updates.append((initial_h3, h3_t))
        extra_updates.append((initial_kappa, k_t))
        extra_updates.append((initial_w, w_t))

        first_sample = function(
            [initial_x, transcripts, transcripts_mask],
            [new_x, pi, phi_t, a_t], updates=updates + extra_updates)

        return first_sample

    def sample_model(self, phrase, phrase_mask, num_samples, num_steps):

        old_x = numpy.zeros((num_samples, self.num_freq + 2))

        one_step = self.sample_one_step(num_samples)

        results = numpy.zeros(
            (num_steps, num_samples, self.num_freq + 2), dtype=floatX)

        for step in range(num_steps):
            print "Step: ", step
            new_x = one_step(old_x, phrase, phrase_mask)
            old_x = new_x
            results[step] = new_x

        return results


class SimpleParrot(Initializable):
    def __init__(
            self,
            num_freq=257,
            k=20,
            k_f0=5,
            rnn1_h_dim=400,
            att_size=10,
            num_letters=28,
            readouts_dim=200,
            sampling_bias=0.,
            **kwargs):

        super(SimpleParrot, self).__init__(**kwargs)

        self.num_freq = num_freq
        self.k = k
        self.rnn1_h_dim = rnn1_h_dim
        self.att_size = att_size
        self.num_letters = num_letters
        self.sampling_bias = sampling_bias
        self.readouts_dim = readouts_dim
        self.attention_mult = 1. / 20.

        self.rnn1_cell1 = GatedRecurrent(dim=rnn1_h_dim, name='rnn1_cell1')

        self.inp_to_h1 = Fork(
            output_names=['rnn1_cell1_inputs', 'rnn1_cell1_gates'],
            input_dim=num_freq + 2,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='inp_to_h1')

        self.h1_to_readout = Linear(
            input_dim=rnn1_h_dim,
            output_dim=readouts_dim,
            name='h1_to_readout')

        self.h1_to_att = Fork(
            output_names=['alpha', 'beta', 'kappa'],
            input_dim=rnn1_h_dim,
            output_dims=[att_size] * 3,
            name='h1_to_att')

        self.att_to_h1 = Fork(
            output_names=['rnn1_cell1_inputs', 'rnn1_cell1_gates'],
            input_dim=num_letters,
            output_dims=[rnn1_h_dim, 2 * rnn1_h_dim],
            name='att_to_h1')

        self.att_to_readout = Linear(
            input_dim=num_letters,
            output_dim=readouts_dim,
            name='att_to_readout')

        self.f0_emitter = F0Emitter(
            input_dim=readouts_dim,
            k_f0=k_f0,
            sampl_bias=sampling_bias)

        self.spectrum_emitter = GMMEmitter(
            input_dim=readouts_dim,
            dim=num_freq,
            k=k,
            sampl_bias=sampling_bias)

        self.children = [
            self.rnn1_cell1,
            self.inp_to_h1,
            self.h1_to_readout,
            self.h1_to_att,
            self.att_to_h1,
            self.att_to_readout,
            self.f0_emitter,
            self.spectrum_emitter]

    def symbolic_input_variables(self):
        f0 = tensor.matrix('f0')
        voiced = tensor.matrix('voiced')
        start_flag = tensor.scalar('start_flag')
        spectrum = tensor.tensor3('spectrum')
        transcripts = tensor.imatrix('transcripts')
        transcripts_mask = tensor.matrix('transcripts_mask')
        f0_mask = tensor.matrix('f0_mask')

        return f0, f0_mask, voiced, spectrum, \
            transcripts, transcripts_mask, start_flag

    def initial_states(self, batch_size):
        initial_h1 = shared_floatx_zeros((batch_size, self.rnn1_h_dim))
        initial_kappa = shared_floatx_zeros((batch_size, self.att_size))
        initial_w = shared_floatx_zeros((batch_size, self.num_letters))

        return initial_h1, initial_kappa, initial_w

    def symbolic_initial_states(self):
        initial_h1 = tensor.matrix('initial_h1')
        initial_h2 = tensor.matrix('initial_h1')
        initial_h3 = tensor.matrix('initial_h1')
        initial_kappa = tensor.matrix('initial_h1')
        initial_w = tensor.matrix('initial_h1')

        return initial_h1, initial_h2, initial_h3, initial_kappa, initial_w

    def numpy_initial_states(self, batch_size):
        initial_h1 = numpy.zeros((batch_size, self.rnn1_h_dim))
        initial_kappa = numpy.zeros((batch_size, self.att_size))
        initial_w = numpy.zeros((batch_size, self.num_letters))

        return initial_h1, initial_kappa, initial_w

    def compute_cost(
            self, f0, f0_mask, voiced, spectrum, transcripts, transcripts_mask,
            start_flag, batch_size, seq_length):

        f0_pr = tensor.shape_padright(f0)
        voiced_pr = tensor.shape_padright(voiced)

        data = tensor.concatenate([spectrum, f0_pr, voiced_pr], 2)

        x = data[:-1]
        # target = data[1:]
        mask = f0_mask[1:]

        target_f0 = f0[1:]
        target_voiced = voiced[1:]
        target_spectrum = spectrum[1:]

        xinp_h1, xgat_h1 = self.inp_to_h1.apply(x)
        transcripts_oh = one_hot(transcripts, self.num_letters) * \
            tensor.shape_padright(transcripts_mask)

        initial_h1, initial_kappa, initial_w = \
            self.initial_states(batch_size)

        # size of transcripts: = transcripts.shape[1]
        u = tensor.shape_padleft(
            tensor.arange(transcripts.shape[1], dtype=floatX), 2)

        def step(xinp_h1_t, xgat_h1_t, h1_tm1, k_tm1, w_tm1, ctx):

            attinp_h1, attgat_h1 = self.att_to_h1.apply(w_tm1)

            h1_t = self.rnn1_cell1.apply(
                xinp_h1_t + attinp_h1,
                xgat_h1_t + attgat_h1, h1_tm1, iterate=False)

            a_t, b_t, k_t = self.h1_to_att.apply(h1_t)

            a_t = tensor.exp(a_t)
            b_t = tensor.exp(b_t)
            k_t = k_tm1 + self.attention_mult * tensor.exp(k_t)

            a_t = tensor.shape_padright(a_t)
            b_t = tensor.shape_padright(b_t)
            k_t_ = tensor.shape_padright(k_t)

            # batch size X att size X len transcripts
            phi_t = tensor.sum(a_t * tensor.exp(-b_t * (k_t_ - u)**2), axis=1)

            # batch size X len transcripts X num letters
            ss6 = tensor.shape_padright(phi_t) * ctx
            w_t = ss6.sum(axis=1)

            return h1_t, k_t, w_t

        (h1, kappa, w), scan_updates = theano.scan(
            fn=step,
            sequences=[xinp_h1, xgat_h1],
            non_sequences=[transcripts_oh],
            outputs_info=[initial_h1, initial_kappa, initial_w])

        readouts = self.h1_to_readout.apply(h1) + \
            self.att_to_readout.apply(w)

        cost_f0 = self.f0_emitter.cost(readouts, target_f0, target_voiced)
        cost_gmm = self.spectrum_emitter.cost(readouts, target_spectrum)

        # cost = self.emitter.cost(readouts, target)
        cost = cost_f0 + cost_gmm
        cost = (cost * mask).sum() / (mask.sum() + 1e-5) + 0. * start_flag
        cost.name = 'nll'

        updates = []
        updates.append((
            initial_h1,
            tensor.switch(start_flag, 0. * initial_h1, h1[-1])))
        updates.append((
            initial_kappa,
            tensor.switch(start_flag, 0. * initial_kappa, kappa[-1])))
        updates.append((
            initial_w,
            tensor.switch(start_flag, 0. * initial_w, w[-1])))

        return cost, scan_updates + updates

    def sample_model_fun(self, context, context_mask, n_steps, num_samples):

        initial_h1, initial_kappa, initial_w = \
            self.initial_states(num_samples)

        initial_x = numpy.zeros((num_samples, self.num_freq + 2))

        context_oh = one_hot(context, self.num_letters) * \
            tensor.shape_padright(context_mask)

        u = tensor.shape_padleft(
            tensor.arange(context.shape[1], dtype=floatX), 2)

        def sample_step(x_tm1, h1_tm1, k_tm1, w_tm1, ctx):
            xinp_h1_t, xgat_h1_t = self.inp_to_h1.apply(x_tm1)
            attinp_h1, attgat_h1 = self.att_to_h1.apply(w_tm1)

            h1_t = self.rnn1_cell1.apply(
                xinp_h1_t + attinp_h1,
                xgat_h1_t + attgat_h1, h1_tm1, iterate=False)

            a_t, b_t, k_t = self.h1_to_att.apply(h1_t)

            a_t = tensor.exp(a_t)
            b_t = tensor.exp(b_t)
            k_t = k_tm1 + tensor.exp(k_t)

            a_t = tensor.shape_padright(a_t)
            b_t = tensor.shape_padright(b_t)
            k_t_ = tensor.shape_padright(k_t)

            # batch size X att size X len context
            phi_t = tensor.sum(a_t * tensor.exp(-b_t * (k_t_ - u)**2), axis=1)

            # batch size X len context X num letters
            w_t = (tensor.shape_padright(phi_t) * ctx).sum(axis=1)

            readout_t = self.h1_to_readout.apply(h1_t) + \
                self.att_to_readout.apply(w_t)

            f0_t, voiced_t = self.f0_emitter.emit(readout_t)
            spectrum_t = self.spectrum_emitter.emit(readout_t)

            # Experiment: Limit the values that can be sampled.
            spectrum_t = tensor.minimum(
                spectrum_t, (spectrum_upper_limit - mean_spectrum) /
                std_spectrum)

            spectrum_t = tensor.maximum(
                spectrum_t, (spectrum_lower_limit - mean_spectrum) /
                std_spectrum)

            x_t = tensor.concatenate([spectrum_t, f0_t, voiced_t], 1)

            mu_t, sigma_t, pi_t = self.spectrum_emitter.components(readout_t)

            return x_t, h1_t, k_t, w_t, pi_t, phi_t, a_t

        (sample_x, h1, k, w, pi, phi, pi_att), updates = theano.scan(
            fn=sample_step,
            n_steps=n_steps,
            sequences=[],
            non_sequences=[context_oh],
            outputs_info=[
                initial_x,
                initial_h1,
                initial_kappa,
                initial_w,
                None, None, None])

        return sample_x, pi, phi, pi_att, updates

    def sample_model(self, phrase, phrase_mask, num_samples, num_steps):

        f0, f0_mask, voiced, spectrum, transcripts, \
            transcripts_mask, start_flag = \
            self.symbolic_input_variables()

        sample_x, sample_pi, sample_phi, sample_pi_att, updates = \
            self.sample_model_fun(
                transcripts, transcripts_mask, num_steps, num_samples)

        return function(
            [transcripts, transcripts_mask],
            [sample_x, sample_pi, sample_phi, sample_pi_att],
            updates=updates)(phrase, phrase_mask)