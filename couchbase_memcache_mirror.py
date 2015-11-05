# Use HasClient to talk to a group of servers
from pymemcache.client.hash import HashClient as McClient
from couchbase.bucket import Bucket as CbBucket
from couchbase.exceptions import NotFoundError, KeyExistsError, NotStoredError
from couchbase.user_constants import FMT_UTF8


class Status(object):
    def __init__(self):
        """
        Multi-return status indicating the success or failure of a given
        Couchbase or Memcached operation.

        Evaluates to a boolean if both completed successfully
        """
        self.cb_error = None
        self.mc_status = True

    def __nonzero__(self):
        return self.cb_error is None and self.mc_status

    @property
    def success(self):
        return bool(self)

    def __repr__(self):
        return 'Status<cb_error={0}, mc_status={1}>'.format(self.cb_error, self.mc_status)


PRIMARY_COUCHBASE = 1
PRIMARY_MEMCACHED = 2


class CouchbaseMemcacheMirror(object):
    def __init__(self, couchbase_uri, memcached_hosts, primary=PRIMARY_COUCHBASE):
        """
        :param couchbase_uri: Connection string for Couchbase
        :param memcached_hosts: List of Memcached nodes
        :param primary: Determines which datastore is authoritative.
            This affects how get operations are performed and which datastore
            is used for CAS operations.
                PRIMARY_COUCHBASE: Couchbase is authoritative
                PRIMARY_MEMCACHED: Memcached is authoritative
            By default, Couchbase is the primary store
        :return:
        """
        self.cb = CbBucket(couchbase_uri)
        self.mc = McClient(memcached_hosts)
        self._primary = primary

    @property
    def primary(self):
        return self._primary

    def _cb_get(self, key):
        try:
            return self.cb.get(key).value
        except NotFoundError:
            return None

    def get(self, key, try_alternate=True):
        """
        Gets a document
        :param key: The key to retrieve
        :param try_alternate: Whether to try the secondary data source if the
            item is not found in the primary.
        :return: The value as a Python object
        """
        if self._primary == PRIMARY_COUCHBASE:
            order = [self._cb_get, self.mc.get]
        else:
            order = [self.mc.get, self._cb_get]

        for meth in order:
            ret = meth(key)
            if ret or not try_alternate:
                return ret

        return None

    def _cb_mget(self, keys):
        """
        Internal method to execute a Couchbase multi-get
        :param keys: The keys to retrieve
        :return: A tuple of {found_key:found_value, ...}, [missing_key1,...]
        """
        try:
            ok_rvs = self.cb.get_multi(keys)
            bad_rvs = {}
        except NotFoundError as e:
            ok_rvs, bad_rvs = e.split_results()

        ok_dict = {k: (v.value, v.cas) for k, v in ok_rvs}
        return ok_dict, bad_rvs.keys()

    def get_multi(self, keys, try_alternate=True):
        """
        Gets multiple items from the server
        :param keys: The keys to fetch as an iterable
        :param try_alternate: Whether to fetch missing items from alternate store
        :return: A dictionary of key:value. Only contains keys which exist and have values
        """
        if self._primary == PRIMARY_COUCHBASE:
            ok, err = self._cb_get(keys)
            if err and try_alternate:
                ok.update(self.mc.get_many(err))
            return ok
        else:
            ok = self.mc.get_many(keys)
            if len(ok) < len(keys) and try_alternate:
                keys_err = set(keys) - set(ok)
                ok.update(self._cb_mget(list(keys_err))[0])
            return ok

    def gets(self, key):
        """
        Get an item with its CAS. The item will always be fetched from the primary
        data store.

        :param key: the key to get
        :return: the value of the key, or None if no such value
        """
        if self._primary == PRIMARY_COUCHBASE:
            try:
                rv = self.cb.get(key)
                return key, rv.cas
            except NotFoundError:
                return None, None
        else:
            return self.mc.gets(key)

    def gets_multi(self, keys):
        if self._primary == PRIMARY_COUCHBASE:
            try:
                rvs = self.cb.get_multi(keys)
            except NotFoundError as e:
                rvs, _ = e.split_results()

            return {k: (v.value, v.cas) for k, v in rvs}
        else:
            # TODO: I'm not sure if this is implemented in HasClient :(
            return self.mc.gets_many(keys)

    def delete(self, key):
        st = Status()
        try:
            self.cb.remove(key)
        except NotFoundError as e:
            st.cb_error = e

        st.mc_status = self.mc.delete(key)
        return st

    def delete_multi(self, keys):
        st = Status()
        try:
            self.cb.remove_multi(keys)
        except NotFoundError as e:
            st.cb_error = e

        st.mc_status = self.mc.delete_many(keys)

    def _do_incrdecr(self, key, value, is_incr):
        cb_value = value if is_incr else -value
        mc_meth = self.mc.incr if is_incr else self.mc.decr
        st = Status()
        try:
            self.cb.counter(key, delta=cb_value)
        except NotFoundError as e:
            st.cb_error = e

        st.mc_status = mc_meth(key, value)

    def incr(self, key, value):
        return self._do_incrdecr(key, value, True)

    def decr(self, key, value):
        return self._do_incrdecr(key, value, False)

    def touch(self, key, expire=0):
        st = Status()
        try:
            self.cb.touch(key, ttl=expire)
        except NotFoundError as e:
            st.cb_error = st

        st.mc_status = self.mc.touch(key)

    def set(self, key, value, expire=0):
        """
        Write first to Couchbase, and then to Memcached
        :param key: Key to use
        :param value: Value to use
        :param expire: If set, the item will expire in the given amount of time
        :return: Status object if successful (will always be success).
                 on failure an exception is raised
        """
        self.cb.upsert(key, value, ttl=expire)
        self.mc.set(key, value, expire=expire)
        return Status()

    def set_multi(self, values, expire=0):
        """
        Set multiple items.
        :param values: A dictionary of key, value indicating values to store
        :param expire: If present, expiration time for all the items
        :return:
        """
        self.cb.upsert_multi(values, ttl=expire)
        self.mc.set_many(values, expire=expire)
        return Status()

    def replace(self, key, value, expire=0):
        """
        Replace existing items
        :param key: key to replace
        :param value: new value
        :param expire: expiration for item
        :return: Status object. Will be OK
        """
        status = Status()
        try:
            self.cb.replace(key, value, ttl=expire)
        except NotFoundError as e:
            status.cb_error = e

        status.mc_status = self.mc.replace(key, value, expire=expire)
        return status

    def add(self, key, value, expire=0):
        status = Status()
        try:
            self.cb.insert(key, value, ttl=expire)
        except KeyExistsError as e:
            status.cb_error = e

        status.mc_status = self.mc.add(key, value, expire=expire)
        return status

    def _append_prepend(self, key, value, is_append):
        cb_meth = self.cb.append if is_append else self.cb.prepend
        mc_meth = self.mc.append if is_append else self.mc.prepend
        st = Status()

        try:
            cb_meth(key, value, format=FMT_UTF8)
        except (NotStoredError, NotFoundError) as e:
            st.cb_error = e

        st.mc_status = mc_meth(key, value)

    def append(self, key, value):
        return self._append_prepend(key, value, True)

    def prepend(self, key, value):
        return self._append_prepend(key, value, False)

    def cas(self, key, value, cas, expire=0):
        if self._primary == PRIMARY_COUCHBASE:
            try:
                self.cb.replace(key, value, cas=cas, ttl=expire)
                self.mc.set(key, value, ttl=expire)
                return True
            except KeyExistsError:
                return False
            except NotFoundError:
                return None
        else:
            return self.mc.cas(key, value, cas)


def main():
    from argparse import ArgumentParser
    import time

    ap = ArgumentParser()
    ap.add_argument('-C', '--couchbase',
                    help='Couchbase connection string',
                    default='couchbase://localhost')
    ap.add_argument('-M', '--memcached',
                    help='List of memcached hosts to use in host:port format',
                    action='append',
                    default=[])
    options = ap.parse_args()

    # Get memcached hosts
    mc_hosts = []
    for server in options.memcached:
        host, port = server.split(':')
        port = int(port)
        mc_hosts.append((host, port))

    mirror = CouchbaseMemcacheMirror(options.couchbase, mc_hosts)
    value = {
        'entry': 'Mirror value',
        'updated': time.time()
    }
    mirror.set('mirrkey', value)

    # Create individual clients, to demonstrate
    cb = CbBucket(options.couchbase)
    print 'Value from couchbase: {}'.format(cb.get('mirrkey').value)
    print 'Value from Memcached: {}'.format(McClient(mc_hosts).get('mirrkey'))

if __name__ == "__main__":
    main()