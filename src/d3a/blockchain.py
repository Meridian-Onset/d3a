from collections import namedtuple, defaultdict
from typing import Dict, Mapping, List, Optional  # noqa

from ethereum.common import set_execution_results, mk_block_from_prevstate
from ethereum.consensus_strategy import get_consensus_strategy
from ethereum.genesis_helpers import mk_basic_state
from ethereum.meta import make_head_candidate
from ethereum.pow import chain
from ethereum.pow.ethpow import Miner
from ethereum.state import BLANK_UNCLES_HASH
from ethereum.tools.tester import ABIContract, Chain as BaseChain, base_alloc, a0
from ethereum.utils import sha3, privtoaddr, encode_hex

from d3a.util import get_cached_joined_contract_source


User = namedtuple('User', ('name', 'address', 'privkey'))


class BCUsers:
    def __init__(self, chain, default_balance=10 ** 24):
        self._users = {}
        self._chain = chain
        self._default_balance = default_balance

    def __getitem__(self, username_or_addr):
        user = self._users.get(username_or_addr)
        if not user:
            if username_or_addr.startswith("0x"):
                raise KeyError("User with address {} doesn't exist".format(username_or_addr))
            self._users[username_or_addr] = user = self._mk_user(username_or_addr)
            self._users[user.address] = user
        if not self._chain.head_state.account_exists(user.address):
            self._chain.head_state.set_balance(user.address, self._default_balance)
        return user

    @staticmethod
    def _mk_user(username):
        key = sha3(username)
        user = User(username, privtoaddr(key), key)
        return user


class Chain(BaseChain):
    def __init__(self, time_source):
        self.time_source = time_source
        self.chain = chain.Chain(
            genesis=mk_basic_state(
                base_alloc,
                header={
                    "number": 0,
                    "gas_limit": 10 ** 9,
                    "gas_used": 0,
                    "timestamp": self.time_source().int_timestamp - 1,
                    "difficulty": 1,
                    "uncles_hash": '0x' + encode_hex(BLANK_UNCLES_HASH)
                }),
            reset_genesis=True
        )
        self.cs = get_consensus_strategy(self.chain.env.config)
        self.block = mk_block_from_prevstate(self.chain, timestamp=self.chain.state.timestamp + 1)
        self.head_state = self.chain.state.ephemeral_clone()
        self.cs.initialize(self.head_state, self.block)
        self.last_sender = None
        self.last_tx = None

    def mine(self, number_of_blocks=1, coinbase=a0):
        listeners = self.head_state.log_listeners

        self.cs.finalize(self.head_state, self.block)
        set_execution_results(self.head_state, self.block)
        self.block = Miner(self.block).mine(rounds=100, start_nonce=0)
        assert self.chain.add_block(self.block)
        assert self.head_state.trie.root_hash == self.chain.state.trie.root_hash
        for i in range(1, number_of_blocks):
            b, _ = make_head_candidate(self.chain, timestamp=self.time_source().int_timestamp)
            b = Miner(b).mine(rounds=100, start_nonce=0)
            assert self.chain.add_block(b)
        self.block = mk_block_from_prevstate(self.chain, timestamp=self.time_source().int_timestamp)
        self.head_state = self.chain.state.ephemeral_clone()
        self.cs.initialize(self.head_state, self.block)

        self.head_state.log_listeners = listeners

    def add_listener(self, listener):
        self.head_state.log_listeners.append(listener)


class BlockChainInterface:
    def __init__(self, time_source: callable, default_user_balance=10 ** 24):
        self.chain = Chain(time_source)
        self.chain.add_listener(self._listener_proxy)
        self.users = BCUsers(self.chain, default_user_balance)  # type: Mapping[str, User]
        self.contracts = {}  # type: Dict[str, ABIContract]
        self.listeners = defaultdict(list)  # type: Dict[str, List[callable]]

    def _listener_proxy(self, log):
        """Translate raw `Log` instances into dict repr before calling the target listener"""
        for listener in self.listeners[log.address]:
            listener(self.contracts[log.address].translator.listen(log))

    def init_contract(self, contract_name: str, args: list, listeners: Optional[List] = None,
                      id_: str = None) -> ABIContract:
        contract = self.chain.contract(
            get_cached_joined_contract_source(contract_name),
            args,
            language='solidity',
        )
        self.contracts[contract.address] = contract
        if id_:
            self.contracts[id_] = contract
        if listeners:
            self.listeners[contract.address].extend(listeners)
        return contract
