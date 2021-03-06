from collections import Iterable
from slm_lab.agent.memory.base import Memory
from slm_lab.lib import logger, util
from slm_lab.lib.decorator import lab_api
import numpy as np
import copy


class OnPolicyReplay(Memory):
    '''
    Stores agent experiences and returns them in a batch for agent training.

    An experience consists of
        - state: representation of a state
        - action: action taken.
                - One hot encoding (discrete)
                - Real numbers representing mean on action dist (continuous)
        - reward: scalar value
        - next state: representation of next state (should be same as state)
        - done: 0 / 1 representing if the current state is the last in an episode
        - priority (optional): scalar value, unnormalized

    The memory does not have a fixed size. Instead the memory stores data from N episodes, where N is determined by the user. After N episodes, all of the examples are returned to the agent to learn from.

    When the examples are returned to the agent, the memory is cleared to prevent the agent from learning from off policy experiences. This memory is intended for on policy algorithms.

    Differences vs. Replay memory:
        - Experiences are nested into episodes. In Replay experiences are flat, and episode is not tracked
        - The entire memory constitues a batch. In Replay batches are sampled from memory.
        - The memory is cleared automatically when a batch is given to the agent.
    '''

    def __init__(self, body):
        super(OnPolicyReplay, self).__init__(body)
        self.state_dim = self.body.state_dim
        self.action_dim = self.body.action_dim
        self.num_epis_to_collect = self.body.agent.spec['algorithm']['num_epis_to_collect']
        # Don't want total experiences reset when memory is
        self.total_experiences = 0
        self.last_nan_idxs = None
        self.nan_idxs = None
        self.memory_warn_flag = True
        self.reset()

    @lab_api
    def reset(self):
        '''Resets the memory. Also used to initialize memory vars'''
        self.states = []
        self.actions = []
        self.rewards = []
        self.next_states = []
        self.dones = []
        self.priorities = []
        self.current_episode = {'states': [],
                                'actions': [],
                                'rewards': [],
                                'next_states': [],
                                'dones': [],
                                'priorities': []}
        self.most_recent = [None, None, None, None, None, None]
        self.true_size = 0  # Size of the current memory
        self.last_nan_idxs = self.nan_idxs
        self.nan_idxs = []
        self.memory_warn_flag = True

    @lab_api
    def update(self, action, reward, state, done):
        '''Interface method to update memory'''
        if not np.isnan(reward):
            self.add_experience(self.last_state, action, reward, state, done)
            self.nan_idxs.append(0)
        else:
            self.nan_idxs.append(1)
            logger.debug2(f'Nan reward')
        self.last_state = state
        '''Clear bodies state buffer for recurent nets'''
        if done:
            self.body.state_buffer = []

    def add_experience(self,
                       state,
                       action,
                       reward,
                       next_state,
                       done,
                       priority=1):
        '''Interface helper method for update() to add experience to memory'''
        self.current_episode['states'].append(state)
        self.current_episode['actions'].append(action)
        self.current_episode['rewards'].append(reward)
        self.current_episode['next_states'].append(next_state)
        self.current_episode['dones'].append(done)
        self.current_episode['priorities'].append(priority)
        # Set most recent
        self.most_recent[0] = state
        self.most_recent[1] = action
        self.most_recent[2] = reward
        self.most_recent[3] = next_state
        self.most_recent[4] = done
        self.most_recent[5] = priority
        # If episode ended, add to memory and clear current_episode
        if done:
            self.states.append(self.current_episode['states'])
            self.actions.append(self.current_episode['actions'])
            self.rewards.append(self.current_episode['rewards'])
            self.next_states.append(self.current_episode['next_states'])
            self.dones.append(self.current_episode['dones'])
            self.priorities.append(self.current_episode['priorities'])
            self.current_episode = {'states': [],
                                    'actions': [],
                                    'rewards': [],
                                    'next_states': [],
                                    'dones': [],
                                    'priorities': []}
            # If agent has collected the desired number of episodes, it is ready to train
            if len(self.states) == self.num_epis_to_collect:
                self.body.agent.algorithm.to_train = 1
        # Track memory size and num experiences
        self.true_size += 1
        if self.true_size > 1000 and self.memory_warn_flag:
            logger.warn("Large memory size: {}".format(self.true_size))
            self.memory_warn_flag = False
        self.total_experiences += 1

    def get_most_recent_experience(self):
        '''Returns the most recent experience'''
        return self.most_recent

    def sample(self):
        '''
        Returns all the examples from memory in a single batch
        Batch is stored as a dict.
        Keys are the names of the different elements of an experience. Values are nested lists of the corresponding sampled elements. Elements are nested into episodes
        e.g.
            batch = {'states'      : [[s_epi1],[s_epi2],...],
                     'actions'     : [[a_epi1],[a_epi2],...],
                     'rewards'     : [[r_epi1],[r_epi2],...],
                     'next_states' : [[ns_epi1],[ns_epi2],...],
                     'dones'       : [[d_epi1],[d_epi2],...],
                     'priorities'  : [[p_epi1],[p_epi2],...]}
        '''
        batch = {}
        batch['states'] = self.states
        batch['actions'] = self.actions
        batch['rewards'] = self.rewards
        batch['next_states'] = self.next_states
        batch['dones'] = self.dones
        batch['priorities'] = self.priorities
        self.reset()
        return batch


class OnPolicyNStepReplay(OnPolicyReplay):
    '''
    Same as OnPolicyReplay Memory but returns the last `length_history` states and next_states for input to a recurrent network.
    Experiences with less than `length_history` previous examples are padded with a 0 valued state and action vector.
    '''
    def __init__(self, body):
        super(OnPolicyNStepReplay, self).__init__(body)
        self.length_history = self.body.agent.spec['memory']['length_history']

    def sample(self):
        '''
        Returns all the examples from memory in a single batch
        Batch is stored as a dict.
        Keys are the names of the different elements of an experience. Values are nested lists of the corresponding sampled elements. Elements are nested into episodes
        states and next_states have are further nested into sequences containing the previous `length_history` - 1 relevant states
        e.g.
            batch = {'states'      : [[[0,...,s0],[0,..,s0,s1],...,[s(k-lh),...,s(k-1),sk]],
                                      [[0,...,s0],[0,..,s0,s1],...,[s(k-lh),...,s(k-1),sk]],
                                      ...,]
                     'actions'     : [[a_epi1],[a_epi2],...],
                     'rewards'     : [[r_epi1],[r_epi2],...],
                     'next_states' : [[[0,...,ns0],[0,..,ns0,ns1],...,[ns(k-lh),...,ns(k-1),nsk]],
                                      [[0,...,ns0],[0,..,ns0,ns1],...,[ns(k-lh),...,ns(k-1),nsk]],
                                      ...,]
                     'dones'       : [[d_epi1],[d_epi2],...],
                     'priorities'  : [[p_epi1],[p_epi2],...]}
        '''
        batch = {}
        batch['states'] = self.add_history(self.states)
        batch['actions'] = self.actions
        batch['rewards'] = self.rewards
        batch['next_states'] = self.add_history(self.next_states)
        batch['dones'] = self.dones
        batch['priorities'] = self.priorities
        self.reset()
        return batch

    def add_history(self, data):
        '''Adds previous self.length_history steps to data'''
        '''Adds previous self.length_history steps to data'''
        all_epi_data_with_history = []
        for epi in data:
            data_with_history = []
            pad_data = copy.deepcopy(epi)
            PAD = np.zeros_like(epi[0])
            for i in range(self.length_history - 1):
                pad_data.insert(0, PAD)
            for i in range(len(epi)):
                if i == len(epi) - 1:
                    data_with_history.append(pad_data[i:])
                else:
                    data_with_history.append(pad_data[i:i + self.length_history])
            all_epi_data_with_history.append(data_with_history)
        return all_epi_data_with_history


class OnPolicyBatchReplay(OnPolicyReplay):
    '''
    Same as OnPolicyReplay Memory with the following difference.

    The memory does not have a fixed size. Instead the memory stores data from N experiences, where N is determined by the user. After N experiences or if an episode has ended, all of the examples are returned to the agent to learn from.

    In contrast, OnPolicyReplay stores entire episodes and stores them in a nested structure. OnPolicyBatchReplay stores experiences in a flat structure.
    '''

    def __init__(self, body):
        self.body = body
        self.last_state = None
        self.state_dim = self.body.state_dim
        self.action_dim = self.body.action_dim
        self.training_frequency = self.body.agent.spec['algorithm']['training_frequency']
        # Don't want total experiences reset when memory is
        self.total_experiences = 0
        self.last_nan_idxs = None
        self.nan_idxs = None
        self.reset()

    def add_experience(self,
                       state,
                       action,
                       reward,
                       next_state,
                       done,
                       priority=1):
        '''Interface helper method for update() to add experience to memory'''
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.next_states.append(next_state)
        self.dones.append(done)
        self.priorities.append(priority)
        # Set most recent
        self.most_recent[0] = state
        self.most_recent[1] = action
        self.most_recent[2] = reward
        self.most_recent[3] = next_state
        self.most_recent[4] = done
        self.most_recent[5] = priority
        # Track memory size and num experiences
        self.true_size += 1
        if self.true_size > 1000 and self.memory_warn_flag:
            logger.warn("Large memory size: {}".format(self.true_size))
            self.memory_warn_flag = False
        self.total_experiences += 1
        # Decide if agent is to train
        if done or (len(self.states)) == self.training_frequency:
            self.body.agent.algorithm.to_train = 1

    def sample(self):
        '''
        Returns all the examples from memory in a single batch
        Batch is stored as a dict.
        Keys are the names of the different elements of an experience. Values are a list of the corresponding sampled elements
        e.g.
            batch = {'states'      : states,
                     'actions'     : actions,
                     'rewards'     : rewards,
                     'next_states' : next_states,
                     'dones'       : dones,
                     'priorities'  : priorities}
        '''
        return super(OnPolicyBatchReplay, self).sample()


class OnPolicyNStepBatchReplay(OnPolicyBatchReplay):
    '''
    Same as OnPolicyBatchReplay Memory but returns the last `length_history` states and next_states for input to a recurrent network.
    Experiences with less than `length_history` previous examples are padded with a 0 valued state and action vector.
    '''
    def __init__(self, body):
        super(OnPolicyNStepBatchReplay, self).__init__(body)
        self.length_history = self.body.agent.spec['memory']['length_history']

    def sample(self):
        '''
        Returns all the examples from memory in a single batch
        Batch is stored as a dict.
        Keys are the names of the different elements of an experience. Values are a list of the corresponding sampled elements.
        States and actions are lists of lists where each sublist corresponds to the kth - length_history (lh) to kth state or action.
        e.g.
            batch = {'states'      : [[0,...,s0],[0,..,s0,s1],...,[s(k-lh),...,s(k-1),sk]],
                     'actions'     : actions,
                     'rewards'     : rewards,
                     'next_states' : [[0,...,ns0],[0,..,ns0,ns1],...,[ns(k-lh),...,ns(k-1),nsk]],
                     'dones'       : dones,
                     'priorities'  : priorities}
        '''
        batch = {}
        batch['states'] = self.add_history(self.states)
        batch['actions'] = self.actions
        batch['rewards'] = self.rewards
        batch['next_states'] = self.add_history(self.next_states)
        batch['dones'] = self.dones
        batch['priorities'] = self.priorities
        self.reset()
        return batch

    def add_history(self, data):
        '''Adds previous self.length_history steps to data'''
        data_with_history = []
        pad_data = copy.deepcopy(data)
        PAD = np.zeros_like(data[0])
        for i in range(self.length_history - 1):
            pad_data.insert(0, PAD)
        for i in range(len(data)):
            if i == len(data) - 1:
                data_with_history.append(pad_data[i:])
            else:
                data_with_history.append(pad_data[i:i + self.length_history])
        return data_with_history
