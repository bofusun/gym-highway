# 3rd party modules
import gym

# internal modules
import gym_highway
from enum import Enum
import pygame
from pygame.math import Vector2


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
            ob, reward, done, _ = env.step(action)
            if done:
                print("Done")
                break
            # Note there's no env.render() here. But the environment still can open window and
            # render if asked by env.monitor: it calls env.render('rgb_array') to record video.
            # Video is not recorded every episode, see capped_cubic_video_schedule for details.

    # Close the env and write monitor result info to disk
    env.close()

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
    main()