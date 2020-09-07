import torch
from torch import nn
import copy
import logging
import numpy as np
from collections import deque

from ginkgo_rl.utils.mcts import MCTSNode
from .base import Agent
from ..utils.nets import MultiHeadedMLP

logger = logging.getLogger(__name__)


class BaseMCTSAgent(Agent):
    def __init__(
        self,
        *args,
        n_mc_target=5,
        n_mc_min=5,
        n_mc_max=100,
        mcts_mode="mean",
        c_puct=1.0,
        reward_range=(-200., 0.),
        verbose=False,
        **kwargs
    ):
        super().__init__(*args, **kwargs)

        self.n_mc_target = n_mc_target
        self.n_mc_min = n_mc_min
        self.n_mc_max = n_mc_max
        self.mcts_mode=mcts_mode
        self.c_puct = c_puct

        self.reward_range = reward_range
        self.verbose = verbose

        self.sim_env = copy.deepcopy(self.env)
        self.sim_env.reset_at_episode_end = False  # Avoids expensive re-sampling of jets every time we parse a path
        self._init_episode()

    def set_env(self, env):
        """ Sets current environment (and initializes episode) """

        self.env = env
        self.sim_env = copy.deepcopy(self.env)
        self.sim_env.reset_at_episode_end = False  # Avoids expensive re-sampling of jets every time we parse a path
        self._init_episode()

    def set_precision(self, n_mc_target, n_mc_min, n_mc_max, mcts_mode, c_puct):
        """ Sets / changes MCTS precision parameters """

        self.n_mc_target = n_mc_target
        self.n_mc_min = n_mc_min
        self.n_mc_max = n_mc_max
        self.mcts_mode = mcts_mode
        self.c_puct = c_puct

    def _predict(self, state):
        action, info = self._mcts(state)
        return action, info

    def update(self, state, reward, action, done, next_state, next_reward, num_episode, **kwargs):
        """ Updates after environment reaction """

        # Keep track of total reward
        self.episode_reward += next_reward
        if self.verbose > 0: logger.debug(f"Agent acknowledges receiving a reward of {next_reward}, episode reward so far {self.episode_reward}")

        # MCTS updates
        if done:
            # Reset MCTS when done with an episode
            self._init_episode()
        else:
            # Update MCTS tree when deciding on an action
            self.mcts_head = self.mcts_head.children[action]
            self.mcts_head.prune()  # This updates the node.path

        # Memorize step
        if self.training:
            self.history.store(log_prob=kwargs["log_prob"], reward=reward)

        loss = 0.0
        if self.training and done:
            # Training
            loss = self._train()

            # Reset memory for next episode
            self.history.clear()

        return loss

    def _parse_path(self, state, path):
        """ Given a path (list of actions), computes the resulting environment state and total reward """

        # Store env state state
        if self.sim_env.state is None or not np.all(np.isclose(self.sim_env.state, self.env.state)):
            self.sim_env.set_internal_state(self.env.get_internal_state())

        self.sim_env.verbose = False

        # Follow path
        total_reward = 0.0
        terminal = False

        for action in path:
            state, reward, done, info = self.sim_env.step(action)
            total_reward += reward

            if done:
                terminal = True
                break

        state = self._tensorize(state)
        return state, total_reward, terminal

    def _parse_action(self, action, from_which_env="sim"):
        """ Given a state and an action, computes the log likelihood """

        if from_which_env == "real":  # Start in self.env state
            if self.sim_env.state is None or not np.all(np.isclose(self.sim_env.state, self.env.state)):
                self.sim_env.set_internal_state(self.env.get_internal_state())
        elif from_which_env == "sim":  # Use current state of self.sim_env
            pass
        else:
            raise ValueError(mode)

        self.sim_env.verbose = False

        try:
            _, _ = action
            log_likelihood = self.sim_env._compute_log_likelihood(action)
        except TypeError:
            log_likelihood = self.sim_env._compute_log_likelihood(self.sim_env.unwrap_action(action))

        return log_likelihood

    def _init_episode(self):
        """ Initializes MCTS tree and total reward so far """

        self.mcts_head = MCTSNode(None, [], reward_min=self.reward_range[0], reward_max=self.reward_range[1])
        self.episode_reward = 0.0

    def _mcts(self, state, max_steps=1000):
        """ Run Monte-Carl tree search from state for n trajectories"""

        if len(self.mcts_head.children) == 1:
            n = 1
        else:
            n_initial_legal_actions = len(self._find_legal_actions(state))
            n = min(max(self.n_mc_target * n_initial_legal_actions - self.mcts_head.n, self.n_mc_min), self.n_mc_max)
        logger.debug(f"Starting MCTS with {n} trajectories")

        for i in range(n):
            if self.verbose > 1: logger.debug(f"Initializing MCTS trajectory {i+1} / {n}")
            node = self.mcts_head
            total_reward = 0.0

            for _ in range(max_steps):
                # Parse current state
                this_state, total_reward, terminal = self._parse_path(state, node.path)
                node.set_terminal(terminal)
                if self.verbose > 1: logger.debug(f"  Node {node.path}")

                # Termination
                if terminal:
                    if self.verbose > 1: logger.debug(f"  Node is terminal")
                    break

                # Expand
                if not node.children:
                    actions = self._find_legal_actions(this_state)
                    step_rewards = [self._parse_action(action, from_which_env="sim") for action in actions]
                    if self.verbose > 1: logger.debug(f"    Expanding: {len(actions)} legal actions")
                    node.expand(actions, step_rewards=step_rewards)

                # Select
                policy_probs = self._evaluate_policy(this_state, node.children.keys(), step_rewards=node.children_q_steps())
                action = node.select_puct(policy_probs, mode=self.mcts_mode, c_puct=self.c_puct)
                if self.verbose > 1: logger.debug(f"    Selecting action {action}")
                node = node.children[action]

            # Backup
            if self.verbose > 1: logger.debug(f"  Backing up total reward of {total_reward}")
            node.give_reward(self.episode_reward + total_reward, backup=True)

        # Select best action
        action = self.mcts_head.select_best(mode="max")
        info = {"log_prob": torch.log(self._evaluate_policy(state, self._find_legal_actions(state), step_rewards=self.mcts_head.children_q_steps(), action=action))}

        # Debug output
        if self.verbose > 0: self._report_decision(action, state)

        return action, info

    def _report_decision(self, chosen_action, state, label="MCTS"):
        legal_actions = self._find_legal_actions(state)
        probs = self._evaluate_policy(state, legal_actions)

        logger.debug(f"{label} results:")
        for i, (action_, node_) in enumerate(self.mcts_head.children.items()):
            is_chosen = '*' if action_ == chosen_action else ' '
            is_greedy = 'g' if action_ == np.argmax(self.mcts_head.children_q_steps()) else ' '
            logger.debug(
                f" {is_chosen}{is_greedy} {action_:>2d}: "
                f"log likelihood = {node_.q_step:6.2f}, "
                f"policy = {probs[i].detach().item():.2f}, "
                f"n = {node_.n:>2d}, "
                f"mean = {node_.q / (node_.n + 1.e-9):>5.1f} [{node_.get_reward():>4.2f}], "
                f"max = {node_.q_max:>5.1f} [{node_.get_reward(mode='max'):>4.2f}]"
            )

    def _evaluate_policy(self, state, legal_actions, step_rewards=None, action=None):
        """ Evaluates the policy on the state and returns the probabilities for a given action or all legal actions """
        raise NotImplementedError

    def _train(self):
        """ Policy updates at end of episode and returns loss """
        raise NotImplementedError


class RandomMCTSAgent(BaseMCTSAgent):
    def _evaluate_policy(self, state, legal_actions, action=None):
        """ Evaluates the policy on the state and returns the probabilities for a given action or all legal actions """
        if action is not None:
            torch.tensor(1. / len(legal_actions), dtype=self.dtype)
        else:
            return 1. / len(legal_actions) * torch.ones(len(legal_actions), dtype=self.dtype)

    def _train(self):
        return 0.0


class MCTSAgent(BaseMCTSAgent):
    def __init__(self, *args, log_likelihood_feature=True, hidden_sizes=(100,100,), activation=nn.ReLU(), **kwargs):
        super().__init__(*args, **kwargs)

        self.log_likelihood_feature = log_likelihood_feature

        self.actor = MultiHeadedMLP(1 + int(self.log_likelihood_feature) + self.state_length, hidden_sizes=hidden_sizes, head_sizes=(1,), activation=activation, head_activations=(None,))
        self.softmax = nn.Softmax(dim=0)

    def _evaluate_policy(self, state, legal_actions, step_rewards=None, action=None):
        batch_states = self._batch_state(state, legal_actions, step_rewards=step_rewards)
        (probs,) = self.actor(batch_states)
        probs = self.softmax(probs).flatten()

        if action is not None:
            assert action in legal_actions
            return probs[legal_actions.index(action)]

        return probs

    def _batch_state(self, state, legal_actions, step_rewards=None):
        state_ = state.view(-1)

        if step_rewards is None:
            step_rewards = [None for _ in legal_actions]
        batch_states = []

        for action, log_likelihood in zip(legal_actions, step_rewards):
            action_ = torch.tensor([action]).to(self.device, self.dtype)

            if self.log_likelihood_feature:
                if log_likelihood is None:
                    log_likelihood = self._parse_action(action, from_which_env="real")
                log_likelihood = np.clip(log_likelihood, self.reward_range[0], self.reward_range[1])
                log_likelihood_ = torch.tensor([log_likelihood]).to(self.device, self.dtype)
                batch_states.append(torch.cat((action_, log_likelihood_, state_), dim=0).unsqueeze(0))
            else:
                batch_states.append(torch.cat((action_, state_), dim=0).unsqueeze(0))

        batch_states = torch.cat(batch_states, dim=0)
        return batch_states

    def _train(self):
        # Roll out last episode
        rollout = self.history.rollout()
        log_probs = torch.stack(rollout["log_prob"], dim=0)

        # Compute loss: train policy to get closer to (deterministic) MCS choice
        loss = -torch.sum(log_probs)

        # Gradient update
        self._gradient_step(loss)

        return loss.item()


class MCBSAgent(MCTSAgent):
    """
    Beam search / MCTS hybrid

    Runs beam search, then MCTS
    """

    def __init__(self, *args, beam_size=10, **kwargs):
        super().__init__(*args, **kwargs)
        self.beam_size = beam_size

    def _predict(self, state):
        self._greedy(state)  # Greedy
        self._beam_search(state, beam_size=self.beam_size)  # Beam search
        action, info = self._mcts(state)
        return action, info

    def _greedy(self, state):
        """ Expands MCTS tree using a greedy algorithm """

        node = self.mcts_head
        if self.verbose > 1: logger.debug(f"Starting greedy algorithm.")

        while not node.terminal:
            # Parse current state
            this_state, total_reward, terminal = self._parse_path(state, node.path)
            node.set_terminal(terminal)
            if self.verbose > 1: logger.debug(f"  Analyzing node {node.path}")

            # Expand
            if not node.terminal and not node.children:
                actions = self._find_legal_actions(this_state)
                step_rewards = [self._parse_action(action, from_which_env="sim") for action in actions]
                if self.verbose > 1: logger.debug(f"    Expanding: {len(actions)} legal actions")
                node.expand(actions, step_rewards=step_rewards)

            # If terminal, backup reward
            if node.terminal:
                if self.verbose > 1: logger.debug(f"    Node is terminal")
                if self.verbose > 1: logger.debug(f"    Backing up total reward {total_reward}")
                node.give_reward(self.episode_reward + total_reward, backup=True)

            # Greedily select next action
            if not node.terminal:
                action = node.select_greedy()
                node = node.children[action]

        if self.verbose > 0:
            choice = self.mcts_head.select_best(mode="max")
            self._report_decision(choice, state, "Greedy")


    def _beam_search(self, state, beam_size):
        """ Expands MCTS tree using beam search """

        beam = [(self.episode_reward, self.mcts_head)]
        next_beam = []

        def format_beam():
            return [node.path for _, node in beam]

        if self.verbose > 1: logger.debug(f"Starting beam search with beam size {beam_size}. Initial beam: {format_beam()}")

        while beam or next_beam:
            for i, (_, node) in enumerate(beam):
                # Parse current state
                this_state, total_reward, terminal = self._parse_path(state, node.path)
                node.set_terminal(terminal)
                if self.verbose > 1: logger.debug(f"  Analyzing node {i+1} / {len(beam)} on beam: {node.path}")

                # Expand
                if not node.terminal and not node.children:
                    actions = self._find_legal_actions(this_state)
                    step_rewards = [self._parse_action(action, from_which_env="sim") for action in actions]
                    if self.verbose > 1: logger.debug(f"    Expanding: {len(actions)} legal actions")
                    node.expand(actions, step_rewards=step_rewards)

                # If terminal, backup reward
                if node.terminal:
                    if self.verbose > 1: logger.debug(f"    Node is terminal")
                    if self.verbose > 1: logger.debug(f"    Backing up total reward {total_reward}")
                    node.give_reward(self.episode_reward + total_reward, backup=True)

                # Did we already process this one? Then skip it
                n_beam_children = node.count_beam_children()
                if n_beam_children >= min(beam_size, len(node)):
                    if self.verbose > 1: logger.debug(f"    Already beam searched this node sufficiently")
                    continue
                else:
                    if self.verbose > 1: logger.debug(f"    So far {n_beam_children} children were beam searched")

                # Beam search selection
                for action in node.select_beam_search(beam_size, exclude_beam_tagged=True):
                    next_reward = total_reward + node.children[action].q_step
                    next_node = node.children[action]
                    next_beam.append((next_reward, next_node))

                # Mark as visited
                node.in_beam = True

            # Just keep top entries for next step
            beam = sorted(next_beam, key=lambda x: x[0], reverse=True)[:beam_size]
            if self.verbose > 1: logger.debug(f"Preparing next step, keeping {beam_size} / {len(next_beam)} nodes in beam: {format_beam()}")
            next_beam = []

        logger.debug(f"Finished beam search")

        if self.verbose > 0:
            choice = self.mcts_head.select_best(mode="max")
            self._report_decision(choice, state, "Beam search")
