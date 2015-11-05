# Python Couchbase Memcached Mirror

This python module provides a `CouchbaseMemcacheMirror` class which allows
mirroring of data between Couchbase and Memcached. It uses the
[pymemcache](https://github.com/pinterest/pymemcache) module for Memcached
access and the [couchbase](https://github.com/couchbase/couchbase-python-client)
for Couchbase access.

The class may be used in your code (using a primarily memcached-style API)
to read and write data to and from Memcached and Couchbase. The class will
transparently write to both Couchbase and Memcached clusters.

# Using

To use this module, import it into your code. You can also run a standalone example

    $ pip install -r requirements.txt
    $ python couchbase_memcache_mirror -C couchbase://10.0.0.99 -M mchost1:11211 -M mchost2:11211
    Value from couchbase: {u'entry': u'Mirror value', u'updated': 1446743031.692772}
    Value from Memcached: {'entry': 'Mirror value', 'updated': 1446743031.692772}

# Primary and secondary data sources

This module allows you to select which data source is a primary for read and write
access. This affects `gets`, get` and `cas` operations: `get` operations are performed against
the primary and `cas` operations are performed against the primary (the secondary being ignored)
