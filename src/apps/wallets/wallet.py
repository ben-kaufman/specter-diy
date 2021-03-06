from app import AppError
from platform import maybe_mkdir, delete_recursively
import json
from bitcoin import ec, hashes, script
from bitcoin.networks import NETWORKS
from bitcoin.psbt import DerivationPath
from bitcoin.descriptor import Descriptor
from bitcoin.descriptor.arguments import AllowedDerivation
from bitcoin.transaction import SIGHASH
import hashlib
from .screens import WalletScreen, WalletInfoScreen
from .commands import DELETE, EDIT, MENU, INFO
from gui.screens import Menu
import lvgl as lv

class WalletError(AppError):
    NAME = "Wallet error"


class Wallet:
    """
    Wallet class,
    wrapped=False - native segwit,
    wrapped=True - nested segwit
    """

    GAP_LIMIT = 20

    def __init__(self, desc, path=None, name="Untitled"):
        self.name = name
        self.path = path
        if self.path is not None:
            self.path = self.path.rstrip("/")
            maybe_mkdir(self.path)
        self.descriptor = desc
        # receive and change gap limits
        self.gaps = [self.GAP_LIMIT for b in range(self.descriptor.num_branches)]
        self.name = name
        self.unused_recv = 0
        self.keystore = None

    async def show(self, network, show_screen):
        while True:
            scr = WalletScreen(self, network, idx=self.unused_recv)
            cmd = await show_screen(scr)
            if cmd == MENU:
                buttons = [
                    (INFO, "Show detailed information"),
                    (EDIT, lv.SYMBOL.EDIT + " Change the name"),
                    # value, label,                            enabled, color
                    (DELETE, lv.SYMBOL.TRASH + " Delete wallet", True, 0x951E2D),
                ]
                cmd = await show_screen(Menu(buttons, last=(255, None), title=self.name, note="What do you want to do?"))
                if cmd == 255:
                    continue
                elif cmd == INFO:
                    keys = self.get_key_dicts(network)
                    for k in keys:
                        k["mine"] = True if self.keystore and self.keystore.owns(k["key"]) else False
                    await show_screen(WalletInfoScreen(self.name, self.full_policy, keys, self.is_miniscript))
                    continue
            # other commands go to the wallet manager
            return cmd

    @property
    def is_watchonly(self):
        """Checks if the wallet is watch-only (doesn't control the key) or not"""
        return not (
            any([self.keystore.owns(k) if self.keystore else False for k in self.keys])
            or
            any([k.is_private for k in self.descriptor.keys])
        )

    def save(self, keystore, path=None):
        # wallet has access to keystore only if it's saved or loaded from file
        self.keystore = keystore
        if path is not None:
            self.path = path.rstrip("/")
        if self.path is None:
            raise WalletError("Path is not defined")
        maybe_mkdir(self.path)
        desc = str(self.descriptor)
        keystore.save_aead(self.path + "/descriptor", plaintext=desc.encode())
        obj = {"gaps": self.gaps, "name": self.name, "unused_recv": self.unused_recv}
        meta = json.dumps(obj).encode()
        keystore.save_aead(self.path + "/meta", plaintext=meta)

    def check_network(self, network):
        """
        Checks that all the keys belong to the network (version of xpub and network of private key).
        Returns True if all keys belong to the network, False otherwise.
        """
        for k in self.keys:
            if k.is_extended:
                if k.key.version not in network.values():
                    return False
            elif k.is_private and isinstance(k.key, ec.PrivateKey):
                if k.key.network["wif"] != network["wif"]:
                    return False
        return True

    def wipe(self):
        if self.path is None:
            raise WalletError("I don't know path...")
        delete_recursively(self.path, include_self=True)

    def get_address(self, idx: int, network: str, branch_index=0):
        sc, gap = self.script_pubkey([int(branch_index), idx])
        return sc.address(NETWORKS[network]), gap

    def script_pubkey(self, derivation: list):
        """Returns script_pubkey and gap limit"""
        # derivation can be only two elements
        branch_idx, idx = derivation
        if branch_idx < 0 or branch_idx >= self.descriptor.num_branches:
            raise WalletError("Invalid branch index %d - can be between 0 and %d" % (branch_idx, self.descriptor.num_branches))
        if idx < 0 or idx >= 0x80000000:
            raise WalletError("Invalid index %d" % idx)
        sc = self.descriptor.derive(idx, branch_index=branch_idx).script_pubkey()
        return sc, self.gaps[branch_idx]

    @property
    def fingerprint(self):
        """Fingerprint of the wallet - hash160(descriptor)"""
        return hashes.hash160(str(self.descriptor))[:4]

    def owns(self, tx_out, bip32_derivations, script=None):
        """
        Checks that psbt scope belongs to the wallet.
        """
        # quick check for the scriptpubkey type
        if tx_out.script_pubkey.script_type() != self.descriptor.scriptpubkey_type():
            return False
        # quick check of the script length
        if script and (len(script.data) != self.descriptor.script_len):
            return False
        derivation = self.get_derivation(bip32_derivations)

        # derivation not found
        if derivation is None:
            return False
        # check that script_pubkey matches
        sc, _ = self.script_pubkey(derivation)
        return sc == tx_out.script_pubkey

    def get_derivation(self, bip32_derivations):
        # otherwise we need standard derivation
        for pub in bip32_derivations:
            if len(bip32_derivations[pub].derivation) >= 2:
                der = self.descriptor.check_derivation(bip32_derivations[pub])
                if der is not None:
                    return der

    def update_gaps(self, psbt=None, known_idxs=None):
        gaps = self.gaps
        # update from psbt
        if psbt is not None:
            scopes = []
            for i, inp in enumerate(psbt.inputs):
                if self.owns(psbt.utxo(i), inp.bip32_derivations, inp.witness_script or inp.redeem_script):
                    scopes.append(inp)
            for i, out in enumerate(psbt.outputs):
                if self.owns(psbt.tx.vout[i], out.bip32_derivations, out.witness_script or out.redeem_script):
                    scopes.append(out)
            for scope in scopes:
                res = self.get_derivation(scope.bip32_derivations)
                if res is not None:
                    branch_idx, idx = res
                    if idx + self.GAP_LIMIT > gaps[branch_idx]:
                        gaps[branch_idx] = idx + self.GAP_LIMIT + 1
        # update from gaps arg
        if known_idxs is not None:
            for i, gap in enumerate(gaps):
                if known_idxs[i] is not None and known_idxs[i] + self.GAP_LIMIT > gap:
                    gaps[i] = known_idxs[i] + self.GAP_LIMIT
        self.unused_recv = gaps[0] - self.GAP_LIMIT
        self.gaps = gaps

    def fill_psbt(self, psbt, fingerprint):
        """Fills derivation paths in inputs"""
        for scope in psbt.inputs:
            # fill derivation paths
            wallet_key = b"\xfc\xca\x01" + self.fingerprint
            if wallet_key not in scope.unknown:
                continue
            der = scope.unknown[wallet_key]
            wallet_derivation = []
            for i in range(len(der) // 4):
                idx = int.from_bytes(der[4 * i : 4 * i + 4], "little")
                wallet_derivation.append(idx)
            # find keys with our fingerprint
            for key in self.descriptor.keys:
                if key.fingerprint == fingerprint:
                    pub = key.derive(wallet_derivation).get_public_key()
                    # fill our derivations
                    scope.bip32_derivations[pub] = DerivationPath(
                        fingerprint, key.derivation + wallet_derivation
                    )
            # fill script
            scope.witness_script = self.descriptor.derive(*wallet_derivation).witness_script()
            if self.descriptor.sh:
                scope.redeem_script = self.descriptor.derive(*wallet_derivation).redeem_script()

    @property
    def keys(self):
        return self.descriptor.keys

    @property
    def has_private_keys(self):
        return any([k.is_private for k in self.keys])

    def get_key_dicts(self, network):
        keys = [{
            "key": k,
        } for k in self.keys]
        # get XYZ-pubs
        slip132_ver = "xpub"
        canonical_ver = "xpub"
        if self.descriptor.is_pkh:
            if self.descriptor.is_wrapped:
                slip132_ver = "ypub"
            elif self.descriptor.is_segwit:
                slip132_ver = "zpub"
        elif self.descriptor.is_basic_multisig:
            if self.descriptor.is_wrapped:
                slip132_ver = "Ypub"
            elif self.descriptor.is_segwit:
                slip132_ver = "Zpub"
        for k in keys:
            k["is_private"] = k["key"].is_private
            ver = slip132_ver.replace("pub", "prv") if k["is_private"] else slip132_ver
            k["slip132"] = k["key"].to_string(NETWORKS[network][ver])
            ver = canonical_ver.replace("pub", "prv") if k["is_private"] else canonical_ver
            k["canonical"] = k["key"].to_string(NETWORKS[network][ver])
        return keys

    def sign_psbt(self, psbt, sighash=SIGHASH.ALL):
        if not self.has_private_keys:
            return
        # psbt may not have derivation for other keys
        # and in case of WIF key there is no derivation whatsoever
        for i, inp in enumerate(psbt.inputs):
            der = self.get_derivation(inp.bip32_derivations)
            if der is None:
                continue
            branch, idx = der
            derived = self.descriptor.derive(idx, branch_index=branch)
            keys = [k for k in derived.keys if k.is_private]
            for k in keys:
                if k.is_private:
                    psbt.sign_with(k.private_key, sighash)

    @classmethod
    def parse(cls, desc, path=None):
        name = "Untitled"
        if "&" in desc:
            name, desc = desc.split("&")
        w = cls.from_descriptor(desc, path)
        w.name = name
        return w

    @classmethod
    def from_descriptor(cls, desc:str, path):
        # remove checksum if it's there and all spaces
        desc = desc.split("#")[0].replace(" ", "")
        descriptor = Descriptor.from_string(desc)
        no_derivation = all([k.is_extended and k.allowed_derivation is None for k in descriptor.keys])
        if no_derivation:
            for k in descriptor.keys:
                if k.is_extended:
                    # allow /{0,1}/*
                    k.allowed_derivation = AllowedDerivation.default()
        return cls(descriptor, path)

    @classmethod
    def from_path(cls, path, keystore):
        """Loads wallet from the folder"""
        path = path.rstrip("/")
        _, desc = keystore.load_aead(path + "/descriptor")
        w = cls.from_descriptor(desc.decode(), path)
        _, meta = keystore.load_aead(path + "/meta")
        obj = json.loads(meta.decode())
        if "gaps" in obj:
            w.gaps = obj["gaps"]
        if "name" in obj:
            w.name = obj["name"]
        if "unused_recv" in obj:
            w.unused_recv = obj["unused_recv"]
        # wallet has access to keystore only if it's saved or loaded from file
        w.keystore = keystore
        return w

    @property
    def policy(self):
        if self.descriptor.is_segwit:
            p = "Nested Segwit, " if self.descriptor.is_wrapped else "Native Segwit, "
        else:
            p = "Legacy, "
        p += self.descriptor.brief_policy
        return p

    @property
    def full_policy(self):
        if self.descriptor.is_segwit:
            p = "Nested Segwit\n" if self.descriptor.is_wrapped else "Native Segwit\n"
        else:
            p = "Legacy\n"
        pp = self.descriptor.full_policy
        if not self.is_miniscript:
            p += pp
        else:
            p += "Miniscript:\n"+pp.replace(",",", ")
        return p

    @property
    def is_miniscript(self):
        return not (self.descriptor.is_basic_multisig or self.descriptor.is_pkh)

    def __str__(self):
        return "%s&%s" % (self.name, self.descriptor)

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, str(self))
