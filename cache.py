import weakref
import socket
import threading
import logging
import cPickle
import time
import zmq

class Cache:
    nextKeySpaceId = 0
    @classmethod
    def newKeySpaceId(cls):
        cls.nextKeySpaceId += 1
        return cls.nextKeySpaceId
    
    def newKeySpace(self):
        return KeySpace(self, self.newKeySpaceId())

    def get(self, key): 
        raise NotImplementedError
    
    def put(self, key, value):
        raise NotImplementedError

class KeySpace:
    def __init__(self, cache, id):
        self.cache = cache
        self.id = id

    def get(self, key):
        return self.cache.get((id, key))

    def put(self, key, value):
        return self.cache.put((id, key), value)


class WeakReferenceCache(Cache):
    def __init__(self):
        self.map = {}

    def get(self, key):
        return self.map.get(key)

    def put(self, key, value):
        self.map[key] = value 

class SoftReferenceCache(Cache):
    def __init__(self):
        self.map = {}


class BoundedMemoryCache(Cache):
    def __init__(self, maxBytes=512*1024*1024):
        self.maxBytes = maxBytes
        self.currentBytes = 0
        self.map = {}

    def get(self, key):
        return self.map.get(key)

    def put(self, key, value):
        self.map[key] = value

class DiskSplillingCache(BoundedMemoryCache):
    pass 


class SerializingCache(Cache):

    def __init__(self, cache):
        self.bmc = cache

    def get(self, key):
        b = self.bmc.get(key)
        return b and cPickle.loads(b) or None

    def put(self, key, value):
        try:
            v = cPickle.dumps(value)
            self.bmc.put(key, v)
        except Exception, e:
            logging.error("cache key %s err", key)


class CacheTrackerMessage:
    pass

class AddedToCache(CacheTrackerMessage):
    def __init__(self, rddId, partition, host):
        self.rddId = rddId
        self.partition = partition
        self.host = host

class DroppedFromCache(CacheTrackerMessage):
    def __init__(self, rddId, partition, host):
        self.rddId = rddId
        self.partition = partition
        self.host = host

class MemoryCacheLost(CacheTrackerMessage):
    def __init__(self, host):
        self.host = host

class RegisterRDD(CacheTrackerMessage):
    def __init__(self, rddId, numPartitions):
        self.rddId = rddId
        self.numPartitions = numPartitions

class GetCacheLocations(CacheTrackerMessage):
    pass

class StopCacheTracker(CacheTrackerMessage):
    pass

ctx = zmq.Context()
class CacheTrackerServer:
    def __init__(self):
        self.addr = None

    def start(self):
        self.t = threading.Thread(target=self.run)
        self.t.daemon = True
        self.t.start()
        while self.addr is None:
            time.sleep(0.01)

    def stop(self):
        self.t.join()

    def run(self):
        locs = {}
        sock = ctx.socket(zmq.REP)
        port = sock.bind_to_random_port("tcp://0.0.0.0")
        self.addr = "tcp://%s:%d" % (socket.gethostname(), port)
        def reply(msg):
            sock.send(cPickle.dumps(msg))
        while True:
            msg = cPickle.loads(sock.recv())
            if isinstance(msg, RegisterRDD):
                locs[msg.rddId] = [[] for i in range(msg.numPartitions)]
                reply('OK')
            elif isinstance(msg, AddedToCache):
                locs[msg.rddId][msg.partition].append(msg.host)
                reply('OK')
            elif isinstance(msg, DroppedFromCache):
                locs[msg.rddId][msg.partition].remove(msg.host)
                reply('OK')
            elif isinstance(msg, MemoryCacheLost):
                for k,v in locs.iteritems():
                    for l in v:
                        l.remove(msg.host)
                reply('OK')
            elif isinstance(msg, GetCacheLocations):
                reply(locs)
            elif isinstance(msg, StopCacheTracker):
                reply('OK')
                break
        sock.close()

class CacheTrackerClient:
    def __init__(self, addr):
        self.sock = ctx.socket(zmq.REQ)
        self.sock.connect(addr)

    def call(self, msg):
        self.sock.send(cPickle.dumps(msg, -1))
        return cPickle.loads(self.sock.recv())


class CacheTracker:
    def __init__(self, isMaster, theCache, addr=None):
        if isMaster:
            self.server = CacheTrackerServer()
            self.server.start()
            addr = self.server.addr
        
        self.client = CacheTrackerClient(addr)
        
        self.registeredRddIds = set()
        self.cache = theCache.newKeySpace()
        self.loading = set()

    def registerRDD(self, rddId, numPartitions):
        if rddId not in self.registeredRddIds:
            logging.info("Registering RDD ID %d with cache", rddId)
            self.registeredRddIds.add(rddId)
            self.client.call(RegisterRDD(rddId, numPartitions))

    def getLocationsSnapshot(self):
        return self.client.call(GetCacheLocations())

    def getOrCompute(self, rdd, split):
        key = (rdd.id, split.index)
        logging.info("CachedRDD partition key is %s", key)
        while key in self.loading:
            time.sleep(0.01)
        cachedVal = self.cache.get(key)
        if cachedVal is not None:
            logging.info("Found partition in cache!")
            return cachedVal
        self.loading.add(key)
        r = list(rdd.compute(split))
        self.cache.put(key, r)
        self.loading.remove(key)
        host = socket.gethostname()
        self.client.call(AddedToCache(rdd.id, split.index, host))
        return r

    def stop(self):
        self.client.call(StopCacheTracker())
        self.registeredRddIds.clear()
        self.server.stop()

def test():
    logging.basicConfig(level=logging.INFO)
    from context import SparkContext
    sc = SparkContext("local")
    sc.init()
    nums = sc.parallelize(range(100), 10)
    cache = BoundedMemoryCache()
    tracker = CacheTracker(True, cache)
    tracker.registerRDD(nums.id, len(nums.splits))
    split = nums.splits[0]
    print tracker.getOrCompute(nums, split)
    print tracker.getOrCompute(nums, split)
    print tracker.getLocationsSnapshot()
    tracker.stop()

if __name__ == '__main__':
    test()