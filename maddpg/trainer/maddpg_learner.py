import argparse
import multiprocessing
import os
import os.path as osp
import pickle
import sys
import time
from collections import deque

import gym
import numpy as np
import tensorflow as tf
import tensorflow.contrib.layers as layers
from gym.envs.registration import register

import maddpg.common.tf_util as U
import models.config as Config
from baselines import logger
from baselines.common import set_global_seeds
from baselines.common.cmd_util import (common_arg_parser, make_env,
                                       make_vec_env, parse_unknown_args)
from baselines.common.policies import build_policy
from baselines.common.tf_util import get_session
from maddpg.trainer.maddpg import MADDPGAgentTrainer
from models.utils import (activation_str_function, create_results_dir,
                          parse_cmdline_kwargs, save_configs)
from baselines.common.models import get_network_builder

try:
    from mpi4py import MPI
except ImportError:
    MPI = None

def mlp(num_layers=2, num_hidden=64, activation=tf.tanh, layer_norm=False):
    def network_fn(X):
        h = X
        for i in range(num_layers):
            h = layers.fully_connected(h, num_outputs=num_hidden, activation_fn=tf.nn.relu)
        return h
    return network_fn

def create_model(**network_kwargs):
    # create mlp model using custom args
    mlp_network_fn = mlp(**network_kwargs)

    # This model takes as input an observation and returns values of all actions
    def mlp_model(input, num_outputs, scope, reuse=False, num_units=256, rnn_cell=None):
        with tf.variable_scope(scope, reuse=reuse):
            out = mlp_network_fn(input)
            out = layers.fully_connected(out, num_outputs=num_outputs, activation_fn=None)
            return out
    return mlp_model

def get_trainers(env, num_adversaries, obs_shape_n, arglist, **network_kwargs):
    trainers = []
    model = create_model(**network_kwargs)
    trainer = MADDPGAgentTrainer
    for i in range(num_adversaries):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.adv_policy=='ddpg')))
    for i in range(num_adversaries, env.n):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.good_policy=='ddpg')))
    return trainers

def learn(env, 
          total_timesteps, 
          arglist,
          seed=None,
          nb_epochs=None, # with default settings, perform 1M steps total
          nb_epoch_cycles=20,
          nb_rollout_steps=100,
          reward_scale=1.0,
          render=False,
          render_eval=False,
          noise_type='adaptive-param_0.2',
          normalize_returns=False,
          normalize_observations=True,
          critic_l2_reg=1e-2,
          actor_lr=1e-4,
          critic_lr=1e-3,
          popart=False,
          gamma=0.99,
          clip_norm=None,
          nb_train_steps=50, # per epoch cycle and MPI worker,
          nb_eval_steps=100,
          batch_size=64, # per MPI worker
          tau=0.01,
          eval_env=None,
          param_noise_adaption_interval=50,
          save_interval=100,
          num_adversaries=0,
          **network_kwargs):
    
    set_global_seeds(seed)

    if MPI is not None:
        rank = MPI.COMM_WORLD.Get_rank()
    else:
        rank = 0
    
    # 1. Create agent trainers
    # replay buffer, actor and critic are defined for each agent in trainers
    obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
    num_adversaries = min(env.n, arglist.num_adversaries)
    trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist, **network_kwargs)
    print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))

    # 2. define parameter and action noise
    # not done in maddpg, but done in ddpg

    # 3. define action scaling
    # not done in maddpg, but done in ddpg

    # 4. define agent(s)
    # already done defining maddpg models in get_trainers

    # 5. output any useful logging information
    # logger.info('scaling actions by {} before executing in env'.format(max_action))

    
    # 6. get session and initialize all agent variables
    # TODO: might just need to use get_session() if sess already created
    with U.single_threaded_session():
        # Initialize
        U.initialize()

        # Load previous results, if necessary
        # TODO: might need to update this based on how we save model
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)

        obs_n = env.reset()
        nenvs = obs_n.shape[0]
        # ensure the shape of obs is consistent
        assert obs_n.shape == (nenvs, env.n, obs_n.shape[-1])

        # 8. initialize metric tracking parameters
        episode_reward = np.zeros(nenvs, dtype = np.float32) #vector
        episode_step = np.zeros(nenvs, dtype = int) # vector
        episodes = 0 #scalar
        t = 0 # scalar
        epoch = 0

        start_time = time.time()

        epoch_episode_rewards = []
        epoch_episode_steps = []
        epoch_actions = []
        epoch_qs = []
        epoch_episodes = 0

        # training metrics
        loss_metrics = {'q_loss':deque(maxlen=len(trainers)), 
                        'p_loss':deque(maxlen=len(trainers)), 
                        'mean_target_q':deque(maxlen=len(trainers)), 
                        'mean_rew':deque(maxlen=len(trainers)), 
                        'mean_target_q_next':deque(maxlen=len(trainers)), 
                        'std_target_q':deque(maxlen=len(trainers))
                       }

        saver = tf.train.Saver()
        episode_rewards_history = deque(maxlen=100)

        # 9. nested training loop
        print('Starting iterations...')
        total_timesteps = arglist.num_episodes*arglist.max_episode_len
        for epoch in range(nb_epochs):
            for cycle in range(nb_epoch_cycles):

                # 7. reset agents and envs
                # NOTE: since we dont have action and param noise, no agent.reset() required here
                 
                # Perform rollouts.
                for t_rollout in range(nb_rollout_steps):
                    # Predict next action.
                    actions_n = []
                    for i in range(nenvs):
                        # get actions for all agents in current env
                        actions_n.append([agent.action(obs) for agent, obs in zip(trainers,obs_n[i])])
                        
                    # confirm actions_n is nenvs x env.n x len(Action)
                    assert actions_n.shape == (nenvs, env.n, env.action_space.n)
                    
                    # environment step
                    new_obs_n, rew_n, done_n, info_n = env.step(actions_n)

                    # sum of rewards for each env
                    episode_reward += [sum(r) for r in rew_n]
                    episode_step += 1

                    # Book-keeping
                    for i, agent in enumerate(trainers):
                        for b in range(nenvs):
                            # print(obs0[b])
                            # print(action[b])
                            # print(reward[b])

                            # save experience from all envs for each agent
                            agent.experience(obs_n[b][i], actions_n[b][i], rew_n[b][i], new_obs_n[b][i], done_n[b][i], None)
                    obs_n = new_obs_n


                    for d in range(len(done_n)):
                        if any(done[d]):
                            # Episode done.
                            epoch_episode_rewards.append(episode_reward[d])
                            episode_rewards_history.append(episode_reward[d])
                            epoch_episode_steps.append(episode_step[d])
                            episode_reward[d] = 0.
                            episode_step[d] = 0
                            epoch_episodes += 1
                            episodes += 1
                            # if nenvs == 1:
                            #     agent.reset()
                    
                    # update timestep
                    t += 1
                
                # Train.
                epoch_actor_losses = []
                epoch_critic_losses = []
                epoch_adaptive_distances = []
                for t_train in range(nb_train_steps):
                    # TODO: Adapt param noise, if necessary. (not included here)

                    cl, al = agent.train()
                    epoch_critic_losses.append(cl)
                    epoch_actor_losses.append(al)
                    agent.update_target_net()

                loss = None
                for agent in trainers:
                    agent.preupdate()
                    loss = agent.update(trainers, train_step)

                    lossvals = [np.mean(data, axis=0) if isinstance(data, list) else data for data in loss]
                    for (lossval, lossname) in zip(lossvals, agent.loss_names):
                        loss_metrics[lossname].append(lossval)
                
                # TODO: implement evaluate logic (not included here)

            # 10. logging metrics
            duration = time.time() - start_time
            combined_stats = {}
            combined_stats[Config.tensorboard_rootdir+'rollout/return'] = np.mean(epoch_episode_rewards)
            combined_stats[Config.tensorboard_rootdir+'rollout/return_history'] = np.mean(episode_rewards_history)
            combined_stats[Config.tensorboard_rootdir+'rollout/episode_steps'] = np.mean(epoch_episode_steps)
            combined_stats[Config.tensorboard_rootdir+'train/loss_actor'] = np.mean(loss_metrics['p_loss'])
            combined_stats[Config.tensorboard_rootdir+'train/loss_critic'] = np.mean(loss_metrics['q_loss'])
            combined_stats[Config.tensorboard_rootdir+'train/mean_target_q'] = np.mean(loss_metrics['mean_target_q'])
            combined_stats[Config.tensorboard_rootdir+'train/mean_rew'] = np.mean(loss_metrics['mean_rew'])
            combined_stats[Config.tensorboard_rootdir+'train/mean_target_q_next'] = np.mean(loss_metrics['mean_target_q_next'])
            combined_stats[Config.tensorboard_rootdir+'train/std_target_q'] = np.mean(loss_metrics['std_target_q'])
            combined_stats[Config.tensorboard_rootdir+'total/duration'] = duration
            combined_stats[Config.tensorboard_rootdir+'total/steps_per_second'] = float(t) / float(duration)
            combined_stats[Config.tensorboard_rootdir+'total/episodes'] = episodes
            combined_stats[Config.tensorboard_rootdir+'rollout/episodes'] = epoch_episodes
            
            # Evaluation statistics.
            # if eval_env is not None:
            #     combined_stats[Config.tensorboard_rootdir+'eval/return'] = eval_episode_rewards
            #     combined_stats[Config.tensorboard_rootdir+'eval/return_history'] = np.mean(eval_episode_rewards_history)
            #     combined_stats[Config.tensorboard_rootdir+'eval/Q'] = eval_qs
            #     combined_stats[Config.tensorboard_rootdir+'eval/episodes'] = len(eval_episode_rewards)


            combined_stats_sums = np.array([ np.array(x).flatten()[0] for x in combined_stats.values()])
            if MPI is not None:
                combined_stats_sums = MPI.COMM_WORLD.allreduce(combined_stats_sums)

            mpi_size = MPI.COMM_WORLD.Get_size() if MPI is not None else 1
            combined_stats = {k : v / mpi_size for (k,v) in zip(combined_stats.keys(), combined_stats_sums)}

            # Total statistics.
            combined_stats[Config.tensorboard_rootdir+'total/epochs'] = epoch + 1
            combined_stats[Config.tensorboard_rootdir+'total/steps'] = t

            for key in sorted(combined_stats.keys()):
                logger.record_tabular(key, combined_stats[key])

            if rank == 0:
                logger.dump_tabular()
            logger.info('')

            # 11. saving model when required
            if save_interval and (epoch % save_interval == 0 or epoch == 1) and logger.get_dir() and (MPI is None or MPI.COMM_WORLD.Get_rank() == 0):
                checkdir = osp.join(logger.get_dir(), 'checkpoints')
                os.makedirs(checkdir, exist_ok=True)
                savepath = osp.join(checkdir, '%.5i'%epoch)
                print('Saving to', savepath)
                agent.save(savepath)
                U.save_state(savepath, saver=saver)
            
        env.close()
