from pettingzoo.utils.env import AECEnv
from pettingzoo.utils import agent_selector
import pygame
import numpy as np
import gymnasium

from collector.utils.objects import Collector, Point

FPS = 120
SCREEN_WIDTH = 1000
SCREEN_HEIGHT = 1000
# Rendering sizes.
POINT_SIZE = 4
PATH_SIZE = 2
COLLECTOR_SIZE = 4
COLLECTOR_LEN = 5


def env(**kwargs):
    env = raw_env(**kwargs)
    return env


# TODO: Create wrapper that takes n_agents, n_points and a sampler.
# And wrapper that takes either.


class raw_env(AECEnv):
    metadata = {
        "name": "collector",
        "render_modes": ["rgb_array", "human"],
        "is_parrallelizable": False,
        "render_fps": FPS,
    }

    def __init__(
        self,
        point_positions,
        agent_positions,
        max_collect,
        cheat_cost=500,
        caught_probability=0.5,
        render_mode=None,
    ):
        assert (
            render_mode in self.metadata["render_modes"] or render_mode is None
        ), (
            f"render_mode: {render_mode} is not supported. "
            f"Supported modes: {self.metadata['render_modes']}"
        )

        self.seed()

        # Positions should be a np.ndarray w. shape (n, 2) representing
        # n (x, y) coordinates.
        self.point_positions = point_positions
        self.agent_positions = agent_positions
        self.render_mode = render_mode
        self.cheat_cost = cheat_cost
        self.caught_probability = caught_probability

        self.reward_range = (-np.inf, 0)

        self.agents = [f"agent_{i}" for i in range(len(agent_positions))]
        self.possible_agents = self.agents[:]
        self.agent_name_mapping = {
            agent: i for i, agent in enumerate(self.agents)
        }
        self._agent_selector = agent_selector(self.agents)
        self.max_collect = {
            agent: max_collect[i] for i, agent in enumerate(self.agents)
        }

        self.scaling, self.translation = self._compute_scaling_and_translation(
            point_positions, agent_positions, SCREEN_WIDTH, SCREEN_HEIGHT
        )

        self.state_space = self._get_state_space(
            agent_positions, point_positions, SCREEN_WIDTH, SCREEN_HEIGHT
        )
        self.action_spaces = self._get_action_spaces(
            self.agents, len(point_positions)
        )
        self.observation_spaces = self._get_observation_spaces(
            self.agents,
            agent_positions,
            point_positions,
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        )

        # The following are set in reset().
        self.points = None
        self.agent_selection = None
        self.has_reset = False
        self.terminate = False
        self.truncate = False
        # Dicts with agent as key.
        self.rewards = None
        self._cumulative_rewards = None
        self.terminations = None
        self.truncations = None
        self.infos = None
        self.collectors = None
        # TODO: Add step count/frames/iterations?

        # pygame
        self.screen = None
        self.clock = None
        self.surf = None
        self.isopen = False

    def _create_boundary_arrays(self, array_2d, shape):
        """Create arrays with minimum and maximum with same shape as input."""
        boundary_low = np.full(shape, np.min(array_2d, axis=0))
        boundary_high = np.full(shape, np.max(array_2d, axis=0))
        return boundary_low, boundary_high

    def _get_state_space(
        self, agent_positions, point_positions, screen_width, screen_height
    ):
        """Retrieve state space."""
        n_points = point_positions.shape[0]
        point_boundary_low, point_boundary_high = self._create_boundary_arrays(
            point_positions, shape=(n_points, 2)
        )
        boundary_low, boundary_high = self._create_boundary_arrays(
            np.concatenate((agent_positions, point_positions)),
            shape=(len(agent_positions), 2),
        )

        state_space = gymnasium.spaces.Dict(
            {
                "point_positions": gymnasium.spaces.Box(
                    low=point_boundary_low,
                    high=point_boundary_high,
                    dtype=np.float32,
                ),
                "collected": gymnasium.spaces.Box(
                    low=0, high=np.inf, shape=(n_points,), dtype=np.int
                ),
                "collector_positions": gymnasium.spaces.Box(
                    low=boundary_low, high=boundary_high, dtype=np.float32
                ),
                "image": gymnasium.spaces.Box(
                    low=0,
                    high=255,
                    shape=(screen_width, screen_height, 3),
                    dtype=np.uint8,
                ),
            }
        )
        return state_space

    def _get_action_spaces(self, agents, n_points):
        """Retrieve action spaces for all agents.

        Each action is a point to collect (by index).
        """
        action_spaces = {
            agent: gymnasium.spaces.Discrete(n_points) for agent in agents
        }
        return action_spaces

    def _get_observation_spaces(
        self,
        agents,
        agent_positions,
        point_positions,
        screen_width,
        screen_height,
    ):
        """Retrieve observation spaces for all agents.

        Each observation consist of the point positions, points collected,
        agent (incl. inactive) positions, and an image of the environment.
        Note that these are identical for all agents.
        """
        n_points = point_positions.shape[0]
        point_boundary_low, point_boundary_high = self._create_boundary_arrays(
            point_positions, shape=(n_points, 2)
        )
        boundary_low, boundary_high = self._create_boundary_arrays(
            np.concatenate((agent_positions, point_positions)),
            shape=(len(agents), 2),
        )

        observation_spaces = {
            agent: gymnasium.spaces.Dict(
                {
                    "point_positions": gymnasium.spaces.Box(
                        low=point_boundary_low,
                        high=point_boundary_high,
                        dtype=np.float32,
                    ),
                    "collected": gymnasium.spaces.Box(
                        low=0, high=np.inf, shape=(n_points,), dtype=np.int
                    ),
                    "collector_positions": gymnasium.spaces.Box(
                        low=boundary_low, high=boundary_high, dtype=np.float32
                    ),
                    "image": gymnasium.spaces.Box(
                        low=0,
                        high=255,
                        shape=(screen_width, screen_height, 3),
                        dtype=np.uint8,
                    ),
                }
            )
            for agent in agents
        }
        return observation_spaces

    def _compute_scaling_and_translation(
        self, point_positions, agent_positions, screen_width, screen_height
    ):
        """Compute scaling and translation to fit all points and agents on
        screen while preserving aspect ratio of data."""
        pos = np.concatenate((point_positions, agent_positions), axis=0)
        minimum = np.min(pos, axis=0)
        maximum = np.max(pos, axis=0)
        x_min, y_min, x_max, y_max = (
            minimum[0],
            minimum[1],
            maximum[0],
            maximum[1],
        )
        x_range = x_max - x_min
        y_range = y_max - y_min
        if x_range > y_range:
            scaling = screen_width / x_range
            translation = -x_min * scaling
        else:
            scaling = screen_height / y_range
            translation = -y_min * scaling
        return scaling, translation

    def _create_collectors(
        self, agent_positions, agents, scaling, translation
    ):
        """Create collectors for all agents as a dict."""
        collectors = {
            agent: Collector(position, scaling, translation)
            for agent, position in zip(agents, agent_positions)
        }
        return collectors

    def _create_points(self, point_positions, scaling, translation):
        """Create points for all given positions."""
        points = [
            Point(position, scaling, translation)
            for position in point_positions
        ]
        return points

    def _create_image_array(self, surf, size):
        """Create image (numpy) array from pygame surface."""
        scaled_surf = pygame.transform.smoothscale(surf, size)
        return np.transpose(
            np.array(pygame.surfarray.pixels3d(scaled_surf)), axes=(1, 0, 2)
        )

    def cheating_cost(self, point):
        return self.cheat_cost * self.caught_probability

    def reward(self, collector, point):
        # Use Euclidean distance as initial cost.
        reward = np.linalg.norm(collector.position - point.position)
        if point.is_collected():
            reward += self.cheating_cost(point)
        # Negate reward since we are using a cost-based model.
        return -reward

    def _state(self, points, collectors):
        """Retrieve observation of the global environment."""
        state = {
            "point_positions": np.array(
                [point.position for point in points], dtype=np.float32
            ),
            "collected": np.array(
                [point.get_collect_counter() for point in points], dtype=np.int
            ),
            "collector_positions": np.array(
                [collector.position for collector in collectors.values()],
                dtype=np.float32,
            ),
            "image": self._render(render_mode="rgb_array"),
        }
        return state

    def observe(self, agent):
        """Returns the observation an agent currently can make.

        This is identical for all agents.
        """
        # TODO: Warning for api_test /Users/lfwa/Library/Caches/pypoetry/virtualenvs/collector-gjPrMD7k-py3.10/lib/python3.10/site-packages/pettingzoo/test/api_test.py:60: UserWarning: Observation is not NumPy array
        # warnings.warn("Observation is not NumPy array")
        return self._state(self.points, self.collectors)

    def state(self):
        """Returns a global view of the environment."""
        return self._state(self.points, self.collectors)

    def reset(self, seed=None, return_info=False, options=None):
        if seed is not None:
            self.seed(seed)

        self.agents = self.possible_agents[:]
        self._agent_selector.reinit(self.agents)
        self.agent_selection = self._agent_selector.reset()

        self.collectors = self._create_collectors(
            self.agent_positions, self.agents, self.scaling, self.translation
        )
        self.points = self._create_points(
            self.point_positions, self.scaling, self.translation
        )

        self.has_reset = True
        self.terminate = False
        self.truncate = False
        self.rewards = {agent: 0 for agent in self.agents}
        self._cumulative_rewards = {agent: 0 for agent in self.agents}
        self.terminations = {agent: False for agent in self.agents}
        self.truncations = {agent: False for agent in self.agents}
        self.infos = {agent: {} for agent in self.agents}

        obs = self._state(self.points, self.collectors)
        observations = {agent: obs for agent in self.agents}

        if not return_info:
            return observations
        else:
            return observations, self.infos

    def step(self, action):
        assert (
            self.has_reset
        ), "Environment has not been reset yet. Call env.reset() first."

        agent = self.agent_selection
        if self.terminations[agent] or self.truncations[agent]:
            self._was_dead_step(action)
            return

        if not self.action_space(agent).contains(action):
            raise ValueError(f"Action {action} is invalid for agent {agent}.")

        point_to_collect = self.points[action]
        collector = self.collectors[agent]

        reward = self.reward(collector, point_to_collect)

        # Move collector to point position.
        collector.position = point_to_collect.position

        # Only collect point after reward has been calculated.
        collector.collect(point_to_collect)

        if self.render_mode == "human":
            self.render()

        self.rewards[agent] = reward
        self.agent_selection = self._agent_selector.next()
        # TODO: Should we reset cumulative reward for agent?
        self._cumulative_rewards[agent] = 0
        self._accumulate_rewards()

        # Update termination and truncation for agent.
        if (
            self.collectors[agent].total_points_collected
            >= self.max_collect[agent]
        ):
            self.terminations[agent] = True

        self.terminate = all(self.terminations.values())
        self.truncate = all(self.truncations.values())

    def render(self):
        if self.render_mode is None:
            gymnasium.logger.warn(
                (
                    f"No render mode specified, skipping render. Please "
                    "specify render_mode as one of the supported modes "
                    f"{self.metadata['render_modes']} at initialization."
                )
            )
        else:
            return self._render(render_mode=self.render_mode)

    def _render(self, render_mode):
        pygame.font.init()
        if self.screen is None and render_mode == "human":
            pygame.init()
            pygame.display.init()
            self.screen = pygame.display.set_mode(
                (SCREEN_WIDTH, SCREEN_HEIGHT)
            )
        if self.clock is None:
            self.clock = pygame.time.Clock()

        self.surf = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT))

        # Add white background.
        self.surf.fill((255, 255, 255))

        # TODO: All collectors and paths are rendered even for inactive
        # agents. Should this happen? Or should we render inactive agents
        # differently?
        self._render_points(self.surf, self.points, POINT_SIZE)
        self._render_paths(self.surf, self.collectors, PATH_SIZE)
        self._render_collectors(
            self.surf, self.collectors, COLLECTOR_LEN, COLLECTOR_SIZE
        )
        # Flip y-axis since pygame has origin at top left.
        self.surf = pygame.transform.flip(self.surf, flip_x=False, flip_y=True)
        self._render_text(self.surf)

        if render_mode == "human":
            pygame.event.pump()
            self.clock.tick(self.metadata["render_fps"])
            assert self.screen is not None
            self.screen.blit(self.surf, (0, 0))
            pygame.display.update()
        elif render_mode == "rgb_array":
            return self._create_image_array(
                self.surf, (SCREEN_WIDTH, SCREEN_HEIGHT)
            )

    def _render_text(self, surf):
        """Render info text."""
        # TODO: Render each text by itself since whole string will move around due to size differences in character length.
        # FIXME: Replace placeholders!
        # TODO: Number of times a collected has cheated is stored in the collector itself. Also stores unique points collected and total points.
        font = pygame.font.Font(pygame.font.get_default_font(), 20)
        text1 = font.render(
            f"Placeholder",
            True,
            (0, 0, 0),
        )
        text2 = font.render(
            f"Placeholder",
            True,
            (0, 0, 0),
        )
        surf.blit(text1, (10, 10))
        surf.blit(text2, (10, 40))

    def _render_points(self, surf, points, point_size):
        """Render all points as circles"""
        for point in points:
            pygame.draw.circle(
                surf,
                point.color,
                tuple(point.scaled_position),
                point_size,
            )

    def _render_paths(self, surf, collectors, path_size):
        """Render paths taken between collections of points."""
        for collector in collectors.values():
            for i in range(1, len(collector.points)):
                pygame.draw.line(
                    surf,
                    collector.color,
                    collector.points[i - 1].scaled_position,
                    collector.points[i].scaled_position,
                    path_size,
                )

    def _render_collectors(
        self, surf, collectors, collector_len, collector_size
    ):
        """Render all collectors as crosses."""
        for collector in collectors.values():
            pygame.draw.line(
                surf,
                collector.color,
                start_pos=tuple(collector.scaled_position - collector_len),
                end_pos=tuple(collector.scaled_position + collector_len),
                width=collector_size,
            )
            pygame.draw.line(
                surf,
                collector.color,
                start_pos=(
                    collector.scaled_position[0] - collector_len,
                    collector.scaled_position[1] + collector_len,
                ),
                end_pos=(
                    collector.scaled_position[0] + collector_len,
                    collector.scaled_position[1] - collector_len,
                ),
                width=collector_size,
            )

    def observation_space(self, agent):
        return self.observation_spaces[agent]

    def action_space(self, agent):
        return self.action_spaces[agent]

    def seed(self, seed=None):
        """Set random seed."""
        self.rng, seed = gymnasium.utils.seeding.np_random(seed)
        return [seed]

    def close(self):
        """Close pygame display if exists."""
        if self.screen is not None:
            pygame.display.quit()
            self.isopen = False
            pygame.quit()
