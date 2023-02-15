import math

import gymnasium
import networkx as nx
import numpy as np
import pygame
from pettingzoo.utils import agent_selector
from pettingzoo.utils.env import AECEnv

from datacollect.utils.objects import Collector, Point

FPS = 120
SCREEN_WIDTH = 1000
SCREEN_HEIGHT = 1000
# Rendering sizes.
POINT_SIZE = 4
PATH_SIZE = 2
COLLECTOR_SIZE = 4
COLLECTOR_LEN = 5
FONT_SIZE = 20


def env(**kwargs):
    """Creates a graph collector environment.

    Returns:
        pettingzoo.utils.env.AECEnv: Created environment.
    """
    env = raw_env(**kwargs)
    return env


class raw_env(AECEnv):
    """Raw graph collector environment.

    This environment is based on a weighted (possible directed) graph using
    networkx. The graph represents the environment structure and may define
    obstacles by creating nodes with e.g. no connecting edges as well as
    define collectable points. Each agent in the environment defines a
    collector that can move around the graph and collect the defined points.

    Attributes:
        See AECEnv.
    """

    metadata = {
        "name": "graph_collector",
        "render_modes": ["rgb_array", "human"],
        "is_parrallelizable": False,
        "render_fps": FPS,
    }

    def __init__(
        self,
        graph,
        point_labels,
        init_agent_labels,
        # TODO: Alternative to max_collect should be max_moves.
        max_collect,
        # TODO: Might want to allow for custom cost of cheating, e.g., as a
        # function of node name? Then it can be arbitrary since creator should
        # have all information about the graph.
        nodes_per_row=None,
        cheat_cost=500,
        caught_probability=0.5,
        static_graph=True,
        render_mode=None,
    ):
        """Initializes the graph collector environment.

        Args:
            graph (networkx.Graph): Input directed or undirected graph
                defining the environment. Node labels must be a continuous set
                of integers starting at 0.
            point_labels (list[int]): List of node labels to identify
                collectable points.
            init_agent_labels (list[int]): List of node labels to identify
                initial agent positions.
            max_collect (list[int]): List of maximum number of points each
                agent can collect.
            nodes_per_row (int, optional): Number of nodes to display per row.
                Defaults to None.
            cheat_cost (int, optional): Cost of cheating by collecting an
                already collected point. Influences reward for collecting
                points. Defaults to 500.
            caught_probability (float, optional): Probability of getting
                caught cheating. Influences reward for collecting points.
                Defaults to 0.5.
            static_graph (bool, optional): Whether the underlying graph is
                static and never changes. May influence performance of
                policies as e.g. shortest paths will need to be recomputed for
                every action to determine optimal agent movement. Defaults to
                True.
            render_mode (str, optional): Render mode. Supported modes are
                specified in environment's metadata["render_modes"] dict.
                Defaults to None.
        """
        assert (
            render_mode in self.metadata["render_modes"] or render_mode is None
        ), (
            f"render_mode: {render_mode} is not supported. "
            f"Supported modes: {self.metadata['render_modes']}"
        )

        self.seed()

        self.graph = graph
        self.point_labels = point_labels
        self.init_agent_labels = init_agent_labels
        self.render_mode = render_mode
        self.cheat_cost = cheat_cost
        self.caught_probability = caught_probability
        self.static_graph = static_graph

        if nodes_per_row is None:
            nodes_per_row = math.ceil(math.sqrt(len(self.graph.nodes)))
        self.nodes_per_row = nodes_per_row

        self.node_width, self.node_height = self._get_node_shape(
            len(self.graph.nodes),
            self.nodes_per_row,
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        )

        self.reward_range = (-np.inf, 0)

        self.agents = [
            f"agent_{i}" for i in range(len(self.init_agent_labels))
        ]
        self.possible_agents = self.agents[:]
        self.agent_name_mapping = {
            agent: i for i, agent in enumerate(self.agents)
        }
        self._agent_selector = agent_selector(self.agents)
        self.max_collect = {
            agent: max_collect[i] for i, agent in enumerate(self.agents)
        }

        self.action_spaces = self._get_action_spaces(
            self.agents, self.graph.nodes
        )
        self.observation_spaces = self._get_observation_spaces(
            len(self.graph.nodes),
            len(self.point_labels),
            self.agents,
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        )
        self.state_space = self._get_state_space(
            len(self.graph.nodes),
            len(self.point_labels),
            len(self.agents),
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        )

        # The following are set in reset().
        self.iteration = 0
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
        self.cumulative_rewards = None

        # pygame
        self.screen = None
        self.clock = None
        self.surf = None
        self.isopen = False

    def _get_node_shape(
        self, n_nodes, nodes_per_row, screen_width, screen_height
    ):
        """Returns the display width and height of a node.

        Args:
            n_nodes (int): Number of nodes in the graph.
            nodes_per_row (int): Number of nodes to display per row.
            screen_width (int): Width of the display.
            screen_height (int): Height of the display.

        Returns:
            tuple: Tuple containing the display width and height of a node.
        """
        width = screen_width / nodes_per_row
        height = screen_height / math.ceil(n_nodes / nodes_per_row)
        return width, height

    def _get_action_spaces(self, agents, nodes):
        """Retrieves action spaces for all agents.

        Each action is a neighbouring node to move to (by node label).

        Args:
            agents (list[str]): List of agent names.
            nodes (list[int]): List of node labels.

        Returns:
            dict: Dictionary of discrete action spaces.
        """
        action_spaces = {
            agent: gymnasium.spaces.Discrete(len(nodes), start=min(nodes))
            for agent in agents
        }

        def sample(mask=None):
            """Generates a sample from the space.

            A sample is a neighbouring node chosen uniformly at random.

            Args:
                mask (np.ndarray, optional): An optimal mask for if an action
                    can be selected where `1` represents valid actions and `0`
                    invalid or infeasible actions. Defaults to None.

            Returns:
                int: Node label of the randomly sampled neighbouring node.
            """
            agent = self.agent_selection
            assert agent is not None, (
                "Agent is required to sample action but none is selected yet. "
                "Did you call reset() before sampling?"
            )
            assert self.collectors is not None, (
                "Collectors are required to sample action but none are "
                "created yet. Did you call reset() before sampling?"
            )

            collector_node = self.collectors[agent].label
            possible_actions = list(nx.neighbors(self.graph, collector_node))

            if not possible_actions:
                return None

            random_action = self.rng.choice(possible_actions)

            return random_action

        # Replace standard sample method s.t. we check for path validity.
        for action_space in action_spaces.values():
            action_space.sample = sample

        return action_spaces

    def _get_observation_spaces(
        self,
        n_nodes,
        n_points,
        agents,
        screen_width,
        screen_height,
    ):
        """Retrieves observation spaces for all agents.

        Each observation consist of the adjacency matrix of the underlying
        graph, list of the point and agent positions as node labels,
        collected points, an image representing the graph, and an action
        mask representing valid actions for the current agent.

        Args:
            n_nodes (int): Number of nodes in the graph.
            n_points (int): Number of points in the graph.
            agents (list[str]): List of agent names.
            screen_width (int): Width of display screen.
            screen_height (int): Height of display screen.

        Returns:
            dict: Dictionary of observation spaces keyed by agent name.
        """
        observation_spaces = {
            agent: gymnasium.spaces.Dict(
                {
                    # Adjacency matrix representing the underlying graph.
                    "graph": gymnasium.spaces.Box(
                        low=0,
                        high=np.inf,
                        shape=(n_nodes, n_nodes),
                        dtype=np.float64,
                    ),
                    # List of node labels, where points/collectors are located.
                    "point_labels": gymnasium.spaces.Box(
                        low=0, high=n_nodes, shape=(n_points,), dtype=int
                    ),
                    "collector_labels": gymnasium.spaces.Box(
                        low=0, high=n_nodes, shape=(len(agents),), dtype=int
                    ),
                    # No. of times each point has been collected.
                    "collected": gymnasium.spaces.Box(
                        low=0, high=np.inf, shape=(n_points,), dtype=int
                    ),
                    "image": gymnasium.spaces.Box(
                        low=0,
                        high=255,
                        shape=(screen_width, screen_height, 3),
                        dtype=np.uint8,
                    ),
                    # Action mask for the current agent representing valid
                    # actions in the current state.
                    "action_mask": gymnasium.spaces.Box(
                        low=0, high=1, shape=(n_nodes,), dtype=int
                    ),
                }
            )
            for agent in agents
        }
        return observation_spaces

    def _get_state_space(
        self, n_nodes, n_points, n_agents, screen_width, screen_height
    ):
        """Retrieves state space.

        The global state consists of the adjacency matrix of the underlying
        graph, list of the point and agent positions as node labels,
        collected points, and an image representing the graph.

        Args:
            n_nodes (int): Number of nodes in the graph.
            n_points (int): Number of points in the graph.
            n_agents (int): Number of agents.
            screen_width (int): Width of display screen.
            screen_height (int): Height of display screen.

        Returns:
            gymnasium.spaces.Dict: State space.
        """
        state_space = gymnasium.spaces.Dict(
            {
                # Adjacency matrix representing the underlying graph.
                "graph": gymnasium.spaces.Box(
                    low=0,
                    high=np.inf,
                    shape=(n_nodes, n_nodes),
                    dtype=np.float64,
                ),
                # List of node labels, where points/collectors are located.
                "point_labels": gymnasium.spaces.Box(
                    low=0, high=n_nodes, shape=(n_points,), dtype=int
                ),
                "collector_labels": gymnasium.spaces.Box(
                    low=0, high=n_nodes, shape=(n_agents,), dtype=int
                ),
                # No. of times each point has been collected.
                "collected": gymnasium.spaces.Box(
                    low=0, high=np.inf, shape=(n_points,), dtype=int
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

    def _get_node_position(
        self, node_label, nodes_per_row, node_width, node_height
    ):
        """Returns the position of a node to be displayed on the screen.

        Args:
            node_label (int): Node label.
            nodes_per_row (int): No. of nodes per row.
            node_width (int): Display width of a node.
            node_height (int): Display height of a node.

        Returns:
            tuple: (x, y) position of the node (with origin at top-left).
        """
        x = (node_label % nodes_per_row) * node_width
        y = (node_label // nodes_per_row) * node_height
        return (x, y)

    def _create_collectors(self, init_agent_labels, agents):
        """Creates collector for each agent as a dict.

        Args:
            init_agent_labels (list[int]): List of node labels representing
                initial agent positions.
            agents (list[str]): List of agent names.

        Returns:
            dict: Dictionary of collectors keyed by agent name.
        """
        collectors = {
            agent: Collector(
                pos=self._get_node_position(
                    node_label=label,
                    nodes_per_row=self.nodes_per_row,
                    node_width=self.node_width,
                    node_height=self.node_height,
                ),
                scaling=0,
                translation=0,
                label=label,
            )
            for agent, label in zip(agents, init_agent_labels)
        }
        return collectors

    def _create_points(self, point_labels):
        """Creates points from given node labels.

        Args:
            point_labels (list[int]): Point positions.

        Returns:
            dict: Dictionary of points keyed by node labels.
        """
        points = {
            label: Point(
                pos=self._get_node_position(
                    node_label=label,
                    nodes_per_row=self.nodes_per_row,
                    node_width=self.node_width,
                    node_height=self.node_height,
                ),
                scaling=0,
                translation=0,
                label=label,
            )
            for label in point_labels
        }
        return points

    def _create_image_array(self, surf, size):
        """Returns image array from pygame surface.

        Args:
            surf (pygame.Surface): Surface to convert to image array.
            size (tuple): Tuple of (width, height) to scale surface to.

        Returns:
            np.ndarray: Image array.
        """
        scaled_surf = pygame.transform.smoothscale(surf, size)
        return np.transpose(
            np.array(pygame.surfarray.pixels3d(scaled_surf)), axes=(1, 0, 2)
        )

    def cheating_cost(self, point):
        """Cost of cheating by collecting an already collected point.

        Args:
            point (Point): Point for which to compute cheating cost.

        Returns:
            float: Cost of cheating.
        """
        return self.cheat_cost * self.caught_probability

    def reward(self, cur_node, new_node):
        """Returns reward for moving from current node to new node.

        If the new node is a point that has already been collected, we add a
        cost for cheating.

        Note:
            We use a cost-based model, so the reward is the negated cost.

        Args:
            cur_node (int): Node label of current node.
            new_node (int): Node label of new node.

        Raises:
            ValueError: No edge exists between current and new node.

        Returns:
            float: Reward
        """
        try:
            cost = self.graph.adj[cur_node][new_node]["weight"]
        except KeyError:
            raise ValueError(
                f"There is no edge between node {cur_node} and {new_node}. "
                "Reward cannot be calculated."
            )
        if new_node in self.points and self.points[new_node].is_collected():
            cost += self.cheating_cost(self.points[new_node])
        # Return negated cost as reward since we are using a cost-based model.
        return -cost

    def _state(self, graph, points, collectors):
        """Retrieves state of the current global environment.

        Args:
            graph (networkx.Graph): Graph representing the environment.
            points (dict): Dictionary of points keyed by node labels.
            collectors (dict): Dictionary of collectors keyed by agent names.

        Returns:
            dict: Current global state.
        """
        state = {
            "graph": nx.to_numpy_array(graph),
            "point_labels": np.array(
                [point.label for point in points.values()], dtype=int
            ),
            "collector_labels": np.array(
                [collector.label for collector in collectors.values()],
                dtype=int,
            ),
            "collected": np.array(
                [point.get_collect_counter() for point in points.values()],
                dtype=int,
            ),
            "image": self._render(render_mode="rgb_array"),
        }
        return state

    def _get_action_mask(self, agent):
        """Retrieves action mask for a given agent.

        The action mask is an array representing the validity of each action.
        An action is valid if the agent can move to the corresponding node.
        Valid actions are represented by `1`, and invalid actions are
        represented by `0`.

        Args:
            agent (str): Agent name.

        Returns:
            np.ndarray: Action mask.
        """
        action_mask = np.zeros(len(self.graph.nodes), dtype=int)
        cur_node = self.collectors[agent].label
        neighbors = nx.neighbors(self.graph, cur_node)
        for neighbor in neighbors:
            action_mask[neighbor] = 1
        return action_mask

    def observe(self, agent):
        # FIXME: Warning for api_test /Users/lfwa/Library/Caches/pypoetry/
        # virtualenvs/collector-gjPrMD7k-py3.10/lib/python3.10/site-packages/
        # pettingzoo/test/api_test.py:60: UserWarning: Observation is not
        # NumPy array
        # warnings.warn("Observation is not NumPy array")
        obs = self._state(self.graph, self.points, self.collectors)
        obs["action_mask"] = self._get_action_mask(agent)
        return obs

    def state(self):
        return self._state(self.graph, self.points, self.collectors)

    def reset(self, seed=None, return_info=False, options=None):
        if seed is not None:
            self.seed(seed)

        self.agents = self.possible_agents[:]
        self._agent_selector.reinit(self.agents)
        self.agent_selection = self._agent_selector.reset()

        self.collectors = self._create_collectors(
            self.init_agent_labels, self.agents
        )
        self.points = self._create_points(self.point_labels)

        self.iteration = 0
        self.has_reset = True
        self.terminate = False
        self.truncate = False

        self.rewards = {agent: 0 for agent in self.agents}
        self._cumulative_rewards = {agent: 0 for agent in self.agents}
        self.terminations = {agent: False for agent in self.agents}
        self.truncations = {agent: False for agent in self.agents}
        self.infos = {agent: {} for agent in self.agents}
        self.cumulative_rewards = {agent: 0 for agent in self.agents}

        observations = {agent: self.observe(agent) for agent in self.agents}

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
            # Guard against first agent dying first since _was_dead_step()
            # does not update agent_selection when that happens.
            if self.agent_selection not in self.agents and self.agents:
                self.agent_selection = self._agent_selector.next()
            return

        if (
            not self.action_space(agent).contains(action)
            and action is not None
        ):
            raise ValueError(
                f"Action {action} is not in the action space for "
                f"agent {agent}."
            )

        collector = self.collectors[agent]
        cur_node = collector.label
        neighbors = list(nx.neighbors(self.graph, cur_node))

        if action in neighbors and action is not None:
            reward = self.reward(cur_node, action)
            # Move agent to new node.
            collector.move(
                position=self._get_node_position(
                    node_label=action,
                    nodes_per_row=self.nodes_per_row,
                    node_width=self.node_width,
                    node_height=self.node_height,
                ),
                label=action,
            )

            # Check if agent has collected a point.
            if action in self.points:
                collector.collect(self.points[action])
        else:
            reward = 0

        # Update termination and truncation for agent.
        if (
            self.collectors[agent].total_points_collected
            >= self.max_collect[agent]
        ):
            self.terminations[agent] = True

        self.terminate = all(self.terminations.values())
        self.truncate = all(self.truncations.values())
        self.iteration += 1

        self.rewards[agent] = reward
        self.cumulative_rewards[agent] += reward
        # Cumulative reward since agent has last acted.
        self._cumulative_rewards[agent] = 0
        self._accumulate_rewards()

        self.agent_selection = self._agent_selector.next()

        if self.render_mode == "human":
            self.render()

    def render(self):
        if self.render_mode is None:
            gymnasium.logger.warn(
                f"No render mode specified, skipping render. Please "
                "specify render_mode as one of the supported modes "
                f"{self.metadata['render_modes']} at initialization."
            )
        else:
            return self._render(render_mode=self.render_mode)

    def _render(self, render_mode):
        """Renders the environment.

        Args:
            render_mode (str): One of the supported render modes.

        Returns:
            np.ndarray or None: Returns the rendered image if render_mode is
                `rgb_array`, otherwise returns None.
        """
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

        self._render_points(
            surf=self.surf,
            points=self.points,
            node_width=self.node_width,
            node_height=self.node_height,
        )
        self._render_obstacles(
            surf=self.surf,
            nodes=self.graph.nodes,
            nodes_per_row=self.nodes_per_row,
            node_width=self.node_width,
            node_height=self.node_height,
        )
        self._render_paths(
            surf=self.surf,
            collectors=self.collectors,
            node_width=self.node_width,
            node_height=self.node_height,
            path_size=PATH_SIZE,
        )
        self._render_collectors(
            surf=self.surf,
            collectors=self.collectors,
            node_width=self.node_width,
            node_height=self.node_height,
            collector_size=COLLECTOR_SIZE,
        )
        self._render_text(self.surf)
        # TODO: Need to visualize graph edges!
        # Might be difficult if the graph is not just locally connected like
        # intended since connections can go across the screen! Visualization
        # of the graph will also be messed up for arbitrary graphs!

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
        """Renders information text, e.g. stats about environment and actions.

        Args:
            surf (pygame.Surface): Surface to render text on.
        """
        # TODO: Render each text by itself since whole string will move around
        # due to size differences in character length.
        (
            stats,
            overall_total_points_collected,
            overall_unique_points_collected,
            overall_cheated,
        ) = self._get_stats()
        total_reward = sum(self.cumulative_rewards.values())
        font = pygame.font.Font(pygame.font.get_default_font(), FONT_SIZE)
        text1 = font.render(
            f"Iteration: {self.iteration} | Total points collected: {overall_total_points_collected} | Unique points collected: {overall_unique_points_collected} / {len(self.points)} | Cheated: {overall_cheated}",
            True,
            (0, 0, 0),
        )
        text2 = font.render(
            f"Total cumulative reward: {total_reward}",
            True,
            (0, 0, 0),
        )
        surf.blit(text1, (10, 10))
        surf.blit(text2, (10, 40))

    def _get_stats(self):
        """Retrieves stats for all collectors.

        Returns:
            tuple: Tuple of stats.
        """
        stats = {}
        overall_total_points_collected = 0
        overall_unique_points_collected = 0
        overall_cheated = 0
        for agent in self.collectors:
            collector = self.collectors[agent]
            stats[agent] = {
                "total_points_collected": collector.total_points_collected,
                "unique_points_collected": collector.unique_points_collected,
                "cheated": collector.cheated,
            }
            overall_total_points_collected += collector.total_points_collected
            overall_unique_points_collected += (
                collector.unique_points_collected
            )
            overall_cheated += collector.cheated
        return (
            stats,
            overall_total_points_collected,
            overall_unique_points_collected,
            overall_cheated,
        )

    def _render_obstacles(
        self, surf, nodes, nodes_per_row, node_width, node_height
    ):
        """Renders obstacles (nodes w. no neighbors) as black rectangles.

        Args:
            surf (pygame.Surface): Surface to render obstacles on.
            nodes (list): List of nodes.
            nodes_per_row (int): No. of nodes to display per row.
            node_width (int): Display width of a node.
            node_height (int): Display height of a node.
        """
        for node in nodes:
            if any(True for _ in nx.neighbors(self.graph, node)):
                continue
            x, y = self._get_node_position(
                node_label=node,
                nodes_per_row=nodes_per_row,
                node_width=node_width,
                node_height=node_height,
            )
            rect = pygame.Rect(x, y, node_width, node_height)
            pygame.draw.rect(
                surf,
                color=(0, 0, 0),
                rect=rect,
            )

    def _render_points(self, surf, points, node_width, node_height):
        """Renders all points as circles.

        Args:
            surf (pygame.Surface): Surface to render points on.
            points (list[Points]): List of points to render.
            node_width (int): Display width of a node.
            node_height (int): Display height of a node.
        """
        # FIXME: Multiple collectors have taken the same path, only latest
        # will be rendered!
        for point in points.values():
            x, y = point.position
            x += node_width / 2
            y += node_height / 2
            pygame.draw.circle(
                surf, point.color, (x, y), min(node_width / 2, node_height / 2)
            )

    def _render_paths(
        self, surf, collectors, node_width, node_height, path_size
    ):
        """Renders paths taken by collectors.

        Args:
            surf (pygame.Surface): Surface to render paths on.
            collectors (dict): Dict of collectors.
            node_width (int): Display width of a node.
            node_height (int): Display height of a node.
            path_size (int): Render size of paths.
        """
        # FIXME: Multiple collectors have taken the same path, only latest
        # will be rendered!
        for collector in collectors.values():
            for i in range(1, len(collector.path_positions)):
                prev_x, prev_y = collector.path_positions[i - 1]
                prev_x += node_width / 2
                prev_y += node_height / 2
                x, y = collector.path_positions[i]
                x += node_width / 2
                y += node_height / 2
                pygame.draw.line(
                    surf,
                    collector.color,
                    (prev_x, prev_y),
                    (x, y),
                    path_size,
                )

    def _render_collectors(
        self, surf, collectors, node_width, node_height, collector_size
    ):
        """Renders all collectors as crosses.

        Args:
            surf (pygame.Surface): Surface to render collectors on.
            collectors (dict): Dict of collectors.
            node_width (int): Display width of a node.
            node_height (int): Display height of a node.
            collector_size (int): Size of collector cross.
        """
        # FIXME: What if collectors overlap? Then only latest will be rendered!
        for collector in collectors.values():
            pygame.draw.line(
                surf,
                collector.color,
                start_pos=collector.position,
                end_pos=(
                    collector.position[0] + node_width,
                    collector.position[1] + node_height,
                ),
                width=collector_size,
            )
            pygame.draw.line(
                surf,
                collector.color,
                start_pos=(
                    collector.position[0] + node_width,
                    collector.position[1],
                ),
                end_pos=(
                    collector.position[0],
                    collector.position[1] + node_height,
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
        """Close pygame display if it exists."""
        if self.screen is not None:
            pygame.display.quit()
            self.isopen = False
            pygame.quit()
