# 3rd party modules
import gym

# internal modules
import gym_highway
from enum import Enum
import pygame
from pygame.math import Vector2
import numpy as np
import tensorflow as tf

import os.path as osp


class RandomAgent(object):
    """The world's simplest agent!"""
    def __init__(self, action_space):
        self.action_space = action_space

    def act(self, observation, reward, done):
        return self.action_space.sample()

def main():
    env = gym.make('Highway-v0')

    # env = wrappers.Monitor(env, directory=outdir, force=True)
    env.seed(0)
    agent = RandomAgent(env.action_space)

    episode_count = 5
    reward = 0
    done = False

    for i in range(episode_count):
        ob = env.reset()
        while True:
            action = agent.act(ob, reward, done)
            # action = 3
            ob, reward, done, _ = env.step(action)
            print("reward: ",reward)
            # print("Ob: ",ob)
            if done:
                print("Done")
                print("Full run reward: ",reward)
                break
            # Note there's no env.render() here. But the environment still can open window and
            # render if asked by env.monitor: it calls env.render('rgb_array') to record video.
            # Video is not recorded every episode, see capped_cubic_video_schedule for details.

    # Close the env and write monitor result info to disk
    env.close()

def sf01(arr):
    """
    swap and then flatten axes 0 and 1
    """
    s = arr.shape
    return arr.swapaxes(0, 1).reshape(s[0] * s[1], *s[2:])

class Action(Enum):
    LEFT = 0
    RIGHT = 1
    ACCELERATE = 2
    MAINTAIN = 3
    # DECELERATE = 3

    ACTION_LOOKUP = {
        0 : LEFT,
        1 : RIGHT,
        2 : ACCELERATE,
        3 : MAINTAIN,
        # 3 : Action.DECELERATE,
    }

if __name__ == '__main__':
    # pygame.init()
    # pygame.display.set_caption("Car tutorial")
    # width = 1900
    # height = 260
    # screen = pygame.display.set_mode((width, height))
    # clock = pygame.time.Clock()

    # bkgd = pygame.image.load('gym_highway/envs/roadImg.png').convert()
    # print("Img loaded")
    # return bkgd
    # new_arry = np.array([-100,0,0,-25]*4).flatten()
    # print(new_arry)
    # flattened_array = new_arry.flatten()
    # print(flattened_array)

    # myList = [0.30000000000000004, 0.5, 0.20000000000000001]
    # myRoundedList = [ round(elem, 2) for elem in myList ]
    # print(myRoundedList)
    print(Action.LEFT)
    print(Action.ACCELERATE)
    print(Action(3))
    print(Action(1))
    
    # main()