from __future__ import print_function
import numpy as np
import tensorflow as tf
import tensorflow.contrib.rnn as rnn
from gym_highway.envs.constants import constants
# import config as Config
import tensorflow.contrib.slim as slim
from ray.rllib.models import Model
from ray.rllib.models.misc import normc_initializer, get_activation_fn

def normalized_columns_initializer(std=1.0):
    def _initializer(shape, dtype=None, partition_info=None):
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)
    return _initializer


def cosineLoss(A, B, name):
    ''' A, B : (BatchSize, d) '''
    dotprod = tf.reduce_sum(tf.multiply(tf.nn.l2_normalize(A,1), tf.nn.l2_normalize(B,1)), 1)
    loss = 1-tf.reduce_mean(dotprod, name=name)
    return loss


def flatten(x):
    return tf.reshape(x, [-1, np.prod(x.get_shape().as_list()[1:])])


def conv2d(x, num_filters, name, filter_size=(3, 3), stride=(1, 1), pad="SAME", dtype=tf.float32, collections=None):
    with tf.variable_scope(name):
        stride_shape = [1, stride[0], stride[1], 1]
        filter_shape = [filter_size[0], filter_size[1], int(x.get_shape()[3]), num_filters]

        # there are "num input feature maps * filter height * filter width"
        # inputs to each hidden unit
        fan_in = np.prod(filter_shape[:3])
        # each unit in the lower layer receives a gradient from:
        # "num output feature maps * filter height * filter width" /
        #   pooling size
        fan_out = np.prod(filter_shape[:2]) * num_filters
        # initialize weights with random weights
        w_bound = np.sqrt(6. / (fan_in + fan_out))

        w = tf.get_variable("W", filter_shape, dtype, tf.random_uniform_initializer(-w_bound, w_bound),
                            collections=collections)
        b = tf.get_variable("b", [1, 1, 1, num_filters], initializer=tf.constant_initializer(0.0),
                            collections=collections)
        return tf.nn.conv2d(x, w, stride_shape, pad) + b


def deconv2d(x, out_shape, name, filter_size=(3, 3), stride=(1, 1), pad="SAME", dtype=tf.float32, collections=None, prevNumFeat=None):
    with tf.variable_scope(name):
        num_filters = out_shape[-1]
        prevNumFeat = int(x.get_shape()[3]) if prevNumFeat is None else prevNumFeat
        stride_shape = [1, stride[0], stride[1], 1]
        # transpose_filter : [height, width, out_channels, in_channels]
        filter_shape = [filter_size[0], filter_size[1], num_filters, prevNumFeat]

        # there are "num input feature maps * filter height * filter width"
        # inputs to each hidden unit
        fan_in = np.prod(filter_shape[:2]) * prevNumFeat
        # each unit in the lower layer receives a gradient from:
        # "num output feature maps * filter height * filter width"
        fan_out = np.prod(filter_shape[:3])
        # initialize weights with random weights
        w_bound = np.sqrt(6. / (fan_in + fan_out))

        w = tf.get_variable("W", filter_shape, dtype, tf.random_uniform_initializer(-w_bound, w_bound),
                            collections=collections)
        b = tf.get_variable("b", [num_filters], initializer=tf.constant_initializer(0.0),
                            collections=collections)
        deconv2d = tf.nn.conv2d_transpose(x, w, tf.pack(out_shape), stride_shape, pad)
        # deconv2d = tf.reshape(tf.nn.bias_add(deconv2d, b), deconv2d.get_shape())
        return deconv2d


def linear(x, size, name, initializer=None, bias_init=0):
    w = tf.get_variable(name + "/w", [x.get_shape()[1], size], initializer=initializer)
    b = tf.get_variable(name + "/b", [size], initializer=tf.constant_initializer(bias_init))
    return tf.matmul(x, w) + b


def categorical_sample(logits, d):
    value = tf.squeeze(tf.multinomial(logits - tf.reduce_max(logits, [1], keep_dims=True), 1), [1])
    return tf.one_hot(value, d)


def inverseUniverseHead(x, final_shape, nConvs=4):
    ''' universe agent example
        input: [None, 288]; output: [None, 42, 42, 1];
    '''
    print('Using inverse-universe head design')
    bs = tf.shape(x)[0]
    deconv_shape1 = [final_shape[1]]
    deconv_shape2 = [final_shape[2]]
    for i in range(nConvs):
        deconv_shape1.append((deconv_shape1[-1]-1)/2 + 1)
        deconv_shape2.append((deconv_shape2[-1]-1)/2 + 1)
    inshapeprod = np.prod(x.get_shape().as_list()[1:]) / 32.0
    assert(inshapeprod == deconv_shape1[-1]*deconv_shape2[-1])
    # print('deconv_shape1: ',deconv_shape1)
    # print('deconv_shape2: ',deconv_shape2)

    x = tf.reshape(x, [-1, deconv_shape1[-1], deconv_shape2[-1], 32])
    deconv_shape1 = deconv_shape1[:-1]
    deconv_shape2 = deconv_shape2[:-1]
    for i in range(nConvs-1):
        x = tf.nn.elu(deconv2d(x, [bs, deconv_shape1[-1], deconv_shape2[-1], 32],
                        "dl{}".format(i + 1), [3, 3], [2, 2], prevNumFeat=32))
        deconv_shape1 = deconv_shape1[:-1]
        deconv_shape2 = deconv_shape2[:-1]
    x = deconv2d(x, [bs] + final_shape[1:], "dl4", [3, 3], [2, 2], prevNumFeat=32)
    return x


def universeHead(x, nConvs=4):
    ''' universe agent example
        input: [None, 42, 42, 1]; output: [None, 288];
    '''
    print('Using universe head design')
    for i in range(nConvs):
        x = tf.nn.elu(conv2d(x, 32, "l{}".format(i + 1), [3, 3], [2, 2]))
        # print('Loop{} '.format(i+1),tf.shape(x))
        # print('Loop{}'.format(i+1),x.get_shape())
    x = flatten(x)
    return x


def nipsHead(x):
    ''' DQN NIPS 2013 and A3C paper
        input: [None, 84, 84, 4]; output: [None, 2592] -> [None, 256];
    '''
    print('Using nips head design')
    x = tf.nn.relu(conv2d(x, 16, "l1", [8, 8], [4, 4], pad="VALID"))
    x = tf.nn.relu(conv2d(x, 32, "l2", [4, 4], [2, 2], pad="VALID"))
    x = flatten(x)
    x = tf.nn.relu(linear(x, 256, "fc", normalized_columns_initializer(0.01)))
    return x


def natureHead(x):
    ''' DQN Nature 2015 paper
        input: [None, 84, 84, 4]; output: [None, 3136] -> [None, 512];
    '''
    print('Using nature head design')
    x = tf.nn.relu(conv2d(x, 32, "l1", [8, 8], [4, 4], pad="VALID"))
    x = tf.nn.relu(conv2d(x, 64, "l2", [4, 4], [2, 2], pad="VALID"))
    x = tf.nn.relu(conv2d(x, 64, "l3", [3, 3], [1, 1], pad="VALID"))
    x = flatten(x)
    x = tf.nn.relu(linear(x, 512, "fc", normalized_columns_initializer(0.01)))
    return x


def doomHead(x):
    ''' Learning by Prediction ICLR 2017 paper
        (their final output was 64 changed to 256 here)
        input: [None, 120, 160, 1]; output: [None, 1280] -> [None, 256];
    '''
    print('Using doom head design')
    x = tf.nn.elu(conv2d(x, 8, "l1", [5, 5], [4, 4]))
    x = tf.nn.elu(conv2d(x, 16, "l2", [3, 3], [2, 2]))
    x = tf.nn.elu(conv2d(x, 32, "l3", [3, 3], [2, 2]))
    x = tf.nn.elu(conv2d(x, 64, "l4", [3, 3], [2, 2]))
    x = flatten(x)
    x = tf.nn.elu(linear(x, 256, "fc", normalized_columns_initializer(0.01)))
    return x

def deepFCHead(x):
    print("Using Deep FC head")
    x = tf.nn.relu(linear(x, 256, "l1", normalized_columns_initializer(0.01)))
    # x = tf.nn.relu(linear(x, 256, "l2", normalized_columns_initializer(0.01)))
    x = flatten(x)
    x = tf.nn.relu(linear(x, 256, "fc", normalized_columns_initializer(0.01)))
    return x


class LSTMPolicy(object):
    def __init__(self, ob_space, ac_space, designHead='universe'):
        self.x = x = tf.placeholder(tf.float32, [None] + list(ob_space), name='x')
        size = 256
        if designHead == 'nips':
            x = nipsHead(x)
        elif designHead == 'nature':
            x = natureHead(x)
        elif designHead == 'doom':
            x = doomHead(x)
        elif 'tile' in designHead:
            x = universeHead(x, nConvs=2)
        else:
            x = universeHead(x)

        # introduce a "fake" batch dimension of 1 to do LSTM over time dim
        x = tf.expand_dims(x, [0])
        lstm = rnn.rnn_cell.BasicLSTMCell(size, state_is_tuple=True)
        self.state_size = lstm.state_size
        step_size = tf.shape(self.x)[:1]

        c_init = np.zeros((1, lstm.state_size.c), np.float32)
        h_init = np.zeros((1, lstm.state_size.h), np.float32)
        self.state_init = [c_init, h_init]
        c_in = tf.placeholder(tf.float32, [1, lstm.state_size.c], name='c_in')
        h_in = tf.placeholder(tf.float32, [1, lstm.state_size.h], name='h_in')
        self.state_in = [c_in, h_in]

        state_in = rnn.rnn_cell.LSTMStateTuple(c_in, h_in)
        lstm_outputs, lstm_state = tf.nn.dynamic_rnn(
            lstm, x, initial_state=state_in, sequence_length=step_size,
            time_major=False)
        lstm_c, lstm_h = lstm_state
        x = tf.reshape(lstm_outputs, [-1, size])
        self.vf = tf.reshape(linear(x, 1, "value", normalized_columns_initializer(1.0)), [-1])
        self.state_out = [lstm_c[:1, :], lstm_h[:1, :]]

        # [0, :] means pick action of first state from batch. Hardcoded b/c
        # batch=1 during rollout collection. Its not used during batch training.
        self.logits = linear(x, ac_space, "action", normalized_columns_initializer(0.01))
        self.sample = categorical_sample(self.logits, ac_space)[0, :]
        self.probs = tf.nn.softmax(self.logits, dim=-1)[0, :]

        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)
        # tf.add_to_collection('probs', self.probs)
        # tf.add_to_collection('sample', self.sample)
        # tf.add_to_collection('state_out_0', self.state_out[0])
        # tf.add_to_collection('state_out_1', self.state_out[1])
        # tf.add_to_collection('vf', self.vf)

    def get_initial_features(self):
        # Call this function to get reseted lstm memory cells
        return self.state_init

    def act(self, ob, c, h):
        sess = tf.get_default_session()
        return sess.run([self.sample, self.vf] + self.state_out,
                        {self.x: [ob], self.state_in[0]: c, self.state_in[1]: h})

    def act_inference(self, ob, c, h):
        sess = tf.get_default_session()
        return sess.run([self.probs, self.sample, self.vf] + self.state_out,
                        {self.x: [ob], self.state_in[0]: c, self.state_in[1]: h})

    def value(self, ob, c, h):
        sess = tf.get_default_session()
        return sess.run(self.vf, {self.x: [ob], self.state_in[0]: c, self.state_in[1]: h})[0]


class StateActionPredictor(object):
    def __init__(self, phi1_m, phi2_m, asample_m, ob_space, ac_space, designHead='universe'):
        # input: s1,s2: : [None, h, w, ch] (usually ch=1 or 4)
        # asample: 1-hot encoding of sampled action from policy: [None, ac_space]
        # input_shape = [None] + list(ob_space)
        input_shape = (None,) + (16,)
        # self.s1 = phi1 = tf.placeholder(tf.float32, input_shape)
        # self.s2 = phi2 = tf.placeholder(tf.float32, input_shape)
        # self.s1 = phi1 = tf.placeholder(tf.float32, shape=input_shape, name="phi1")
        # self.s2 = phi2 = tf.placeholder(tf.float32, shape=input_shape, name="phi2")
        # self.asample = asample = tf.placeholder(tf.float32, shape=(None, ac_space), name="asample")
        
        self.s1 = phi1 = phi1_m
        self.s2 = phi2 = phi2_m
        self.asample = asample = asample_m

        # feature encoding: phi1, phi2: [None, LEN]
        size = 256
        phi1 = deepFCHead(phi1)
        with tf.variable_scope(tf.get_variable_scope(), reuse=True):
            phi2 = deepFCHead(phi2)

        # inverse model: g(phi1,phi2) -> a_inv: [None, ac_space]
        g = tf.concat([phi1, phi2], 1)
        g = tf.nn.relu(linear(g, size, "g1", normalized_columns_initializer(0.01)))
        aindex = tf.argmax(asample, axis=1)  # aindex: [batch_size,]
        logits = linear(g, ac_space, "glast", normalized_columns_initializer(0.01))

        # Required for TFPolicyGraph
        self.outputs = logits

        self.invloss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(
                                        logits=logits, labels=aindex), name="invloss")
        self.ainvprobs = tf.nn.softmax(logits, dim=-1)

        # print("shape g: ", g.get_shape())
        # print("shape phi1: ", phi1.get_shape())
        # print("shape phi2: ", phi2.get_shape())
        # print("shape asample: ", asample.get_shape())

        # forward model: f(phi1,asample) -> phi2
        # Note: no backprop to asample of policy: it is treated as fixed for predictor training
        f = tf.concat([phi1, asample], 1)
        f = tf.nn.relu(linear(f, size, "f1", normalized_columns_initializer(0.01)))
        f = linear(f, phi1.get_shape()[1].value, "flast", normalized_columns_initializer(0.01))
        self.forwardloss = 0.5 * tf.reduce_mean(tf.square(tf.subtract(f, phi2)), name='forwardloss')
        # self.forwardloss = 0.5 * tf.reduce_mean(tf.sqrt(tf.abs(tf.subtract(f, phi2))), name='forwardloss')
        # self.forwardloss = cosineLoss(f, phi2, name='forwardloss')
        self.forwardloss = self.forwardloss * 256.0  # lenFeatures=288. Factored out to make hyperparams not depend on it.

        # variable list
        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)

    def pred_act(self, s1, s2):
        '''
        returns action probability distribution predicted by inverse model
            input: s1,s2: [h, w, ch]
            output: ainvprobs: [ac_space]
        '''
        sess = tf.get_default_session()
        return sess.run(self.ainvprobs, {self.s1: [s1], self.s2: [s2]})[0, :]

    def pred_bonus(self, sess, s1, s2, asample):
        '''
        returns bonus predicted by forward model
            input: s1,s2: [h, w, ch], asample: [ac_space] 1-hot encoding
            output: scalar bonus
        '''
        # sess = tf.get_default_session()
        # error = sess.run([self.forwardloss, self.invloss],
        #     {self.s1: [s1], self.s2: [s2], self.asample: [asample]})
        # print('ErrorF: ', error[0], ' ErrorI:', error[1])
        error = sess.run(self.forwardloss,
            {self.s1: [s1], self.s2: [s2], self.asample: [asample]})
        error = error * constants['PREDICTION_BETA']
        return error


class StatePredictor(object):
    '''
    Loss is normalized across spatial dimension (42x42), but not across batches.
    It is unlike ICM where no normalization is there across 288 spatial dimension
    and neither across batches.
    '''

    def __init__(self, phi1_m, phi2_m, asample_m, ob_space, ac_space, designHead='universe', unsupType='state'):
        # input: s1,s2: : [None, h, w, ch] (usually ch=1 or 4)
        # asample: 1-hot encoding of sampled action from policy: [None, ac_space]
        # input_shape = [None] + list(ob_space)
        input_shape = (None,) + (16,)
        # self.s1 = phi1 = tf.placeholder(tf.float32, shape=input_shape, name="phi1")
        # self.s2 = phi2 = tf.placeholder(tf.float32, shape=input_shape, name="phi2")
        # self.asample = asample = tf.placeholder(tf.float32, shape=(None, ac_space), name="asample")

        self.s1 = phi1 = phi1_m
        self.s2 = phi2 = phi2_m
        self.asample = asample = asample_m

        self.stateAenc = unsupType == 'stateAenc'

        # feature encoding: phi1: [None, LEN]
        phi1 = deepFCHead(phi1)
        with tf.variable_scope(tf.get_variable_scope(), reuse=True):
            phi2 = deepFCHead(phi2)

        # if designHead == 'universe':
        #     phi1 = universeHead(phi1)
        #     if self.stateAenc:
        #         with tf.variable_scope(tf.get_variable_scope(), reuse=True):
        #             phi2_aenc = universeHead(phi2)
        # elif 'tile' in designHead:  # for mario tiles
        #     phi1 = universeHead(phi1, nConvs=2)
        #     if self.stateAenc:
        #         with tf.variable_scope(tf.get_variable_scope(), reuse=True):
        #             phi2_aenc = universeHead(phi2)
        # else:
        #     print('Only universe designHead implemented for state prediction baseline.')
        #     exit(1)

        # forward model: f(phi1,asample) -> phi2
        # Note: no backprop to asample of policy: it is treated as fixed for predictor training
        f = tf.concat([phi1, asample], 1)
        f = tf.nn.relu(linear(f, phi1.get_shape()[1].value, "f1", normalized_columns_initializer(0.01)))
        if 'tile' in designHead:
            f = inverseUniverseHead(f, input_shape, nConvs=2)
        else:
            f = inverseUniverseHead(f, input_shape)
        self.forwardloss = 0.5 * tf.reduce_mean(tf.square(tf.subtract(f, phi2)), name='forwardloss')
        if self.stateAenc:
            self.aencBonus = 0.5 * tf.reduce_mean(tf.square(tf.subtract(phi1, phi2_aenc)), name='aencBonus')
        self.predstate = phi1

        # variable list
        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)

    def pred_state(self, s1, asample):
        '''
        returns state predicted by forward model
            input: s1: [h, w, ch], asample: [ac_space] 1-hot encoding
            output: s2: [h, w, ch]
        '''
        sess = tf.get_default_session()
        return sess.run(self.predstate, {self.s1: [s1],
                                            self.asample: [asample]})[0, :]

    def pred_bonus(self, sess, s1, s2, asample):
        '''
        returns bonus predicted by forward model
            input: s1,s2: [h, w, ch], asample: [ac_space] 1-hot encoding
            output: scalar bonus
        '''
        # sess = tf.get_default_session()
        bonus = self.aencBonus if self.stateAenc else self.forwardloss
        error = sess.run(bonus,
            {self.s1: [s1], self.s2: [s2], self.asample: [asample]})
        # print('ErrorF: ', error)
        error = error * constants['PREDICTION_BETA']
        return error


# TODO: move config file and convert options into a dict
# NOTE: temp just added the hidden layers and activatins for this model in the constants. 
# but better to move them to the config file later.
class FCPolicy(object):

    def __init__(self,
                 inputs,
                 num_outputs,
                 options,
                 state_in=None,
                 seq_lens=None):
        self.inputs = inputs

        # Default attribute values for the non-RNN case
        self.state_init = []
        self.state_in = state_in or []
        self.state_out = []
        if seq_lens is not None:
            self.seq_lens = seq_lens
        else:
            self.seq_lens = tf.placeholder(
                dtype=tf.int32, shape=[None], name="seq_lens")

        if options.get("free_log_std", False):
            assert num_outputs % 2 == 0
            num_outputs = num_outputs // 2
        self.outputs, self.last_layer = self._build_layers(
            inputs, num_outputs, options)
        if options.get("free_log_std", False):
            log_std = tf.get_variable(
                name="log_std",
                shape=[num_outputs],
                initializer=tf.zeros_initializer)
            self.outputs = tf.concat(
                [self.outputs, 0.0 * self.outputs + log_std], 1)

        # value function
        # self.vf = tf.reshape(linear(self.last_layer, 1, "value", normc_initializer(1.0)),[-1])

        # variable list
        self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)

    def _build_layers(self, inputs, num_outputs, options):
        """Define the layers of a custom model.

        Arguments:
            input_dict (dict): Dictionary of input tensors, including "obs",
                "prev_action", "prev_reward".
            num_outputs (int): Output tensor must be of size
                [BATCH_SIZE, num_outputs].
            options (dict): Model options.
        """
        hiddens = options.get("fcnet_hiddens", constants['FCNET_HIDDENS'])
        activation = get_activation_fn(options.get("fcnet_activation", constants['FCNET_ACTIVATION']))

        with tf.name_scope("fc_net"):
            i = 1
            last_layer = inputs
            for size in hiddens:
                label = "fc{}".format(i)
                last_layer = slim.fully_connected(
                    last_layer,
                    size,
                    weights_initializer=normc_initializer(1.0),
                    activation_fn=activation,
                    scope=label)
                i += 1
            label = "fc_out"
            output = slim.fully_connected(
                last_layer,
                num_outputs,
                weights_initializer=normc_initializer(0.01),
                activation_fn=None,
                scope=label)
            return output, last_layer

    def value_function(self):
        """Builds the value function output.

        This method can be overridden to customize the implementation of the
        value function (e.g., not sharing hidden layers).

        Returns:
            Tensor of size [BATCH_SIZE] for the value function.
        """
        return tf.reshape(
            linear(self.last_layer, 1, "value", normc_initializer(1.0)), [-1])