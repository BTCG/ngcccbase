import hashlib
import hmac
import os

from pycoin.ecdsa.secp256k1 import generator_secp256k1 as BasePoint
from pycoin.encoding import from_bytes_32, public_pair_to_bitcoin_address

from address import AddressRecord, LooseAddressRecord
from asset import AssetDefinition
from coloredcoinlib import ColorSet
import logging
logger = logging.getLogger('ngcccbase')

class DeterministicAddressRecord(AddressRecord):
    """Subclass of AddressRecord which is entirely deterministic.
    DeterministicAddressRecord will use a single master key to
    create addresses for specific colors and bitcoin addresses.
    """
    def __init__(self, **kwargs):
        """Create an address for this color <color_set>
        and index <index> with the master key <master_key>.
        The address record returned for the same three variables
        will be the same every time, hence "deterministic".
        """
        super(DeterministicAddressRecord, self).__init__(**kwargs)
        if len(self.color_set.get_data()) == 0:
            color_string = "genesis block"
        else:
            color_string = self.color_set.get_hash_string()
        self.index = kwargs.get('index')
        self.hmac = hmac.new(str(kwargs['master_key']),
                             "%s|%s" % (color_string, self.index),
                             hashlib.sha256)
        self._rawPrivKey = None
        self._publicPoint = None
        self._address = None

    @property
    def rawPrivKey(self):
        if self._rawPrivKey is None:
            self._rawPrivKey = from_bytes_32(self.hmac.digest())
        return self._rawPrivKey
    
    @property
    def publicPoint(self):
        if self._publicPoint is None:
            self._publicPoint = BasePoint * self.rawPrivKey
        return self._publicPoint

    @property
    def address(self):
        if self._address is None:
            self._address = public_pair_to_bitcoin_address(
                self.publicPoint.pair(),
                compressed=False,
                address_prefix=self.addressprefix
            )
        return self._address


class DWalletAddressManager(object):
    """This class manages the creation of new AddressRecords.
    Specifically, it keeps track of which colors have been created
    in this wallet and how many addresses of each color have been
    created in this wallet.
    """
    def __init__(self, colormap, config):
        """Create a deterministic wallet address manager given
        a colormap <colormap> and a configuration <config>.
        Note address manager configuration is in the key "dwam".
        """
        self.config = config
        self.testnet = self.config.get('testnet', False)
        self.colormap = colormap
        self.addresses = set()

        # initialize the wallet manager if this is the first time
        #  this will generate a master key.
        params = self.config.get('dwam', None)
        if params is None:
            params = self.init_new_wallet()

        # master key is stored in a separate config entry
        self.master_key = self.config['dw_master_key']

        self.genesis_color_sets = params['genesis_color_sets']
        self.color_set_states = params['color_set_states']

        # import addresses
        self._import_genesis_addresses()
        self._import_specific_color_addresses()
        self._import_one_off_addresses_from_config()

    def _import_one_off_addresses_from_config(self):
        logger.debug("Loose address is '%s', addresses from config are %s" % (self.add_loose_address, self.config.get('addresses', []))) 
        map(self.add_loose_address, self.config.get('addresses', []))

    def _import_specific_color_addresses(self):
        for color_set_st in self.color_set_states:
            color_desc_list = color_set_st['color_set']
            max_index = color_set_st['max_index']
            color_set = ColorSet(self.colormap, color_desc_list)
            params = {
                'testnet': self.testnet,
                'master_key': self.master_key,
                'color_set': color_set
            }
            for index in xrange(max_index + 1):
                params['index'] = index
                self.addresses.add(DeterministicAddressRecord(**params))

    def _import_genesis_addresses(self):
        for i, color_desc_list in enumerate(self.genesis_color_sets):
            addr = self.get_genesis_address(i)
            addr.color_set = ColorSet(self.colormap,
                                      color_desc_list)
            self.addresses.add(addr)

    def add_loose_address(self, addr_params):
        addr_params['testnet'] = self.testnet
        addr_params['color_set'] = ColorSet(self.colormap,
                                            addr_params['color_set'])
        logger.debug('Address params are: %s' % addr_params)
        address = LooseAddressRecord(**addr_params)
        self.addresses.add(address)
        return address

    def find_address_by_wif(self, wif):
        for address in list(self.addresses):
            data = address.get_data()
            if wif == data["address_data"]:
                return address
        return None

    def init_new_wallet(self):
        """Initialize the configuration if this is the first time
        we're creating addresses in this wallet.
        Returns the "dwam" part of the configuration.
        """
        if not 'dw_master_key' in self.config:
            master_key = os.urandom(64).encode('hex')
            self.config['dw_master_key'] = master_key
        dwam_params = {
            'genesis_color_sets': [],
            'color_set_states': []
            }
        self.config['dwam'] = dwam_params
        return dwam_params

    def increment_max_index_for_color_set(self, color_set):
        """Given a color <color_set>, record that there is one more
        new address for that color.
        """
        # TODO: speed up, cache(?)
        for color_set_st in self.color_set_states:
            color_desc_list = color_set_st['color_set']
            max_index = color_set_st['max_index']
            cur_color_set = ColorSet(self.colormap,
                                     color_desc_list)
            if cur_color_set.equals(color_set):
                max_index += 1
                color_set_st['max_index'] = max_index
                return max_index
        self.color_set_states.append({"color_set": color_set.get_data(),
                                      "max_index": 0})
        return 0

    def get_new_address(self, asset_or_color_set):
        """Given an asset or color_set <asset_or_color_set>,
        Create a new DeterministicAddressRecord and return it.
        The DWalletAddressManager will keep that tally and
        persist it in storage, so the address will be available later.
        """
        if isinstance(asset_or_color_set, AssetDefinition):
            color_set = asset_or_color_set.get_color_set()
        else:
            color_set = asset_or_color_set
        index = self.increment_max_index_for_color_set(color_set)
        na = DeterministicAddressRecord(master_key=self.master_key,
                                        color_set=color_set, index=index,
                                        testnet=self.testnet)
        self.addresses.add(na)
        self.update_config()
        return na

    def get_genesis_address(self, genesis_index):
        """Given the index <genesis_index>, will return
        the Deterministic Address Record associated with that
        index. In general, that index corresponds to the nth
        color created by this wallet.
        """
        return DeterministicAddressRecord(
            master_key=self.master_key,
            color_set=ColorSet(self.colormap, []),
            index=genesis_index, testnet=self.testnet)

    def get_new_genesis_address(self):
        """Create a new genesis address and return it.
        This will necessarily increment the number of genesis
        addresses from this wallet.
        """
        index = len(self.genesis_color_sets)
        self.genesis_color_sets.append([])
        self.update_config()
        address = self.get_genesis_address(index)
        address.index = index
        self.addresses.add(address)
        return address

    def update_genesis_address(self, address, color_set):
        """Updates the genesis address <address> to have a different
        color set <color_set>.
        """
        assert address.color_set.color_id_set == set([])
        address.color_set = color_set
        self.genesis_color_sets[address.index] = color_set.get_data()
        self.update_config()

    def get_some_address(self, color_set):
        """Returns an address associated with color <color_set>.
        This address will be essentially a random address in the
        wallet. No guarantees to what will come out.
        If there is not address corresponding to the color_set,
        thhis method will create one and return it.
        """
        acs = self.get_addresses_for_color_set(color_set)
        if acs:
            # reuse
            return acs[0]
        else:
            return self.get_new_address(color_set)

    def get_change_address(self, color_set):
        """Returns an address that can receive the change amount
        for a color <color_set>
        """
        return self.get_some_address(color_set)

    def get_all_addresses(self):
        """Returns the list of all AddressRecords in this wallet.
        """
        return list(self.addresses)

    def find_address_record(self, address):
        for address_rec in list(self.addresses):
            if address_rec.get_address() == address:
                return address_rec
        return None

    def get_addresses_for_color_set(self, color_set):
        """Given a color <color_set>, returns all AddressRecords
        that have that color.
        """
        return [addr for addr in list(self.addresses)
                if color_set.intersects(addr.get_color_set())]

    def update_config(self):
        """Updates the configuration for the address manager.
        The data will persist in the key "dwam" and consists
        of this data:
        genesis_color_sets - Colors created by this wallet
        color_set_states   - How many addresses of each color
        """
        dwam_params = {
            'genesis_color_sets': self.genesis_color_sets,
            'color_set_states': self.color_set_states
            }
        self.config['dwam'] = dwam_params
