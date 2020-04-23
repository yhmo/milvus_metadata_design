from init_db import db
SQLALCHEMY_DATABASE_URI='sqlite:////tmp/meta_lab/meta.sqlite?check_same_thread=False'
db.init_db(uri=SQLALCHEMY_DATABASE_URI)

from collections import defaultdict, OrderedDict
from functools import partial
from models import Segments, SegmentCommits, Collections, CollectionSnapshots, SegmentFiles

class Proxy:
    def __init__(self, node, cleanup=None):
        self.node = node
        self.prev = None
        self.next = None
        self.refcnt = 0
        self.cleanupcb = []
        cleanup and self.cleanupcb.append(cleanup)

    def set_next(self, next_node):
        next_node.prev = self
        self.next = next_node
        if self.refcnt == 0:
            self.cleanup()
        return self

    def set_prev(self, prev_node):
        prev_node.next = self
        self.prev = prev_node

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return self.__dict__[attr]
        return getattr(self.node, attr)

    def is_tail(self):
        return self.next == None

    def is_head(self):
        return self.prev == None

    def ref(self):
        self.refcnt += 1

    def unref(self):
        self.refcnt -= 1
        if self.refcnt <= 0:
            self.cleanup()

    def register_cb(self, cb):
        self.cleanupcb.append(cb)

    def cleanup(self):
        # self.cleanupcb and self.cleanupcb(self.node)
        self.do_cleanup()
        for cb in self.cleanupcb:
            cb(self.node)

    def do_cleanup(self):
        pass


class DBProxy(Proxy):
    def __init__(self, node, cleanup=None):
        super().__init__(node, cleanup=cleanup)
        self.model = node.__class__

    def do_cleanup(self):
        print(f'Doing CLEANUP {self.model.__name__} {self.node.id}')
        db.Session.delete(self.node)
        db.Session.commit()


class HirachyDBProxy(DBProxy):
    def __init__(self, node, parent, cleanup=None):
        super().__init__(node, cleanup=cleanup)
        self.parent = parent
        self.parent.ref()

    def do_cleanup(self):
        super().do_cleanup()
        self.parent.unref()


class ResourceMgr:
    level_one_model = None
    level_two_model = None
    link_key = ''
    proxy_class = DBProxy
    def __init__(self):
        self.resources = defaultdict(OrderedDict)

    def cleanupcb(self, target):
        level_one_key = getattr(target, self.link_key)
        print(f'Removing l1={level_one_key} l2={target.id}')
        self.resources[level_one_key].pop(target.id, None)

    def get_level2_records(self, level_one_id, **kwargs):
        records = db.Session.query(self.level_two_model).filter(getattr(self.level_two_model,
            self.link_key)==level_one_id).all()
        return records

    def process_new_level2_records(self, level_one_id, records):
        for record in records:
            proxy = self.proxy_class(record, cleanup=self.cleanupcb)
            self.resources[level_one_id][record.id] = proxy

    def load(self, level_one_id):
        lid = level_one_id
        if isinstance(level_one_id, self.level_one_model):
            lid = level_one_id.id

        records = self.get_level2_records(lid)

        self.process_new_level2_records(lid, records)

        # for k, v in self.resources.items():
        #     print(f'{k} --')
        #     for kk, vv in v.items():
        #         print(f'\t{kk}')

    def get_without_level_two_id(self, level_one_id):
        assert False, 'GetWithoutLevelTwoIDError'
        return False, None

    def get(self, level_one_id, level_two_id=None):
        lid = level_one_id
        if isinstance(level_one_id, self.level_one_model):
            lid = level_one_id.id

        level_one_resource = self.resources.get(lid, None)
        if not level_one_resource:
            self.load(lid)
            level_one_resource = self.resources.get(lid, None)
            if not level_one_resource:
                return None

        if level_two_id is None:
            ret, level_two_id = self.get_without_level_two_id(lid)
            if not ret:
                return

        ss = level_one_resource.get(level_two_id, None)
        # ss and print(f'PRE  Get SS {ss.id} ref={ss.refcnt}')
        ss and ss.ref()
        # ss and print(f'POST Get SS {ss.id} ref={ss.refcnt}')
        return ss

    def release(self, resource):
        resource and resource.unref()


class SegmentFilesMgr(ResourceMgr):
    level_one_model = Collections
    level_two_model = SegmentFiles
    link_key = 'collection_id'


class SegmentsMgr(ResourceMgr):
    level_one_model = Collections
    level_two_model = Segments
    link_key = 'collection_id'


class SegmentsCommitsMgr(ResourceMgr):
    level_one_model = Collections
    level_two_model = SegmentCommits
    link_key = 'collection_id'

    def __init__(self, segment_mgr, segment_files_mgr):
        super().__init__()
        self.segment_mgr = segment_mgr
        self.segment_files_mgr = segment_files_mgr

    def load(self, level_one_id):
        lid = level_one_id
        if isinstance(level_one_id, self.level_one_model):
            lid = level_one_id.id

        records = db.Session.query(self.level_two_model).filter(getattr(self.level_two_model,
            self.link_key)==lid).all()

        def cb(first, second):
            print(f'Unref {first.node.__class__.__name__} {first.id}')
            first.unref()

        for record in records:
            proxy = self.proxy_class(record, cleanup=self.cleanupcb)
            print(f'c {record.id}')
            for file_id in proxy.mappings:
                seg_file = self.segment_files_mgr.get(record.collection_id, file_id)
                proxy.register_cb(partial(cb, seg_file))
                print(f'\tf {seg_file.id}')
            segment = self.segment_mgr.get(record.collection_id, record.segment_id)
            proxy.register_cb(partial(cb, segment))
            self.resources[lid][record.id] = proxy


class SnapshotsMgr(ResourceMgr):
    level_one_model = Collections
    level_two_model = CollectionSnapshots
    link_key = 'collection_id'

    def __init__(self, commits_mgr):
        super().__init__()
        self.heads = {}
        self.tails = {}
        self.keeps = 5
        self.stale_sss = defaultdict(OrderedDict)
        self.commits_mgr = commits_mgr

    def load(self, level_one_id):
        lid = level_one_id
        if isinstance(level_one_id, self.level_one_model):
            lid = level_one_id.id

        def cb(first, second):
            print(f'Unref {first.node.__class__.__name__} {first.id}')
            first.unref()

        # records = db.Session.query(self.level_two_model).filter(getattr(self.level_two_model,
        #     self.link_key)==lid).all()

        records = db.Session.query(CollectionSnapshots).filter(CollectionSnapshots.collection_id==lid
                ).order_by(CollectionSnapshots.id.desc()).all()

        next_node = None
        num = 0
        for record in records:
            num += 1
            proxy = self.proxy_class(record, cleanup=self.cleanupcb)
            print(f'ss {record.id}')
            for commit_id in proxy.mappings:
                commit = self.commits_mgr.get(record.collection.id, commit_id)
                proxy.register_cb(partial(cb, commit))
                print(f'\tcc {commit.id if commit else None} {commit_id}')

            if num > self.keeps:
                proxy.unref()
                continue

            proxy.ref()
            if next_node is not None:
                proxy.set_next(next_node)
            else:
                self.tails[lid] = proxy
            self.resources[lid][record.id] = proxy
            next_node = proxy

        if next_node is not None:
            self.heads[lid] = next_node

        print(f'Len of SS: {len(self.resources[lid])}')

    # def close_snapshots(self, collection):
    #     cid = collection
    #     if isinstance(collection, Collections):
    #         cid = collection.id

    #     self.resources.pop(cid)
    #     self.heads.pop(cid, None)
    #     self.tails.pop(cid, None)

    def get_without_level_two_id(self, level_one_id):
        level_two_id = self.tails[level_one_id].id
        return True, level_two_id

    def stale_cb(self, node):
        print(f'CLEANUP: Removing {node.id} from stale snapshots list')
        self.stale_sss[node.collection.id].pop(node.id, None)

    def mark_as_stale(self, ss):
        self.stale_sss[ss.collection.id][ss.id] = ss
        ss.register_cb(self.stale_cb)
        ss.unref()

    def drop(self, collection):
        cid = collection
        if isinstance(cid, Collections):
            cid = cid.id
        head = self.heads.pop(cid, None)
        if not head:
            return
        self.resources[cid].pop(head.id, None)
        next_node = head.next
        if not next_node:
            return
        next_node.prev = None
        self.heads[cid] = next_node

        self.mark_as_stale(head)

    def append(self, snapshot):
        cid = snapshot.collection
        if isinstance(cid, Collections):
            cid = cid.id
        proxy = self.proxy_class(snapshot, cleanup=self.cleanupcb)
        tail = self.tails.get(cid, None)
        if tail:
            assert proxy.id > tail.id
            proxy.set_prev(tail)
            tail.set_next(proxy)

        self.tails[cid] = proxy
        self.resources[cid][proxy.id] = proxy

        if len(self.resources[cid]) > self.keeps:
            self.drop(cid)

    def active_snapshots(self, collection):
        cid = collection
        if isinstance(cid, Collections):
            cid = cid.id
        return sorted(self.resources[cid].keys()), self.stale_sss[cid].keys()


if __name__ == '__main__':
    import sys
    collection = db.Session.query(Collections).first()
    seg_mgr = SegmentsMgr()
    seg_mgr.load(collection)
    # segment = seg_mgr.get(collection, 2)
    # print(f'Segment cid={segment.collection.id} sid={segment.id}')
    # seg_mgr.release(segment)

    seg_files_mgr = SegmentFilesMgr()
    seg_files_mgr.load(collection)

    seg_commit_mgr = SegmentsCommitsMgr(seg_mgr, seg_files_mgr)
    seg_commit_mgr.load(collection)
    # segment_commit = seg_commit_mgr.get(collection, 6)
    # seg_commit_mgr.release(segment_commit)
    # print(f'Segment cid={segment.collection.id} sid={segment.id}')

    # sys.exit(0)

    ss_mgr = SnapshotsMgr(seg_commit_mgr)
    ss_mgr.load(collection)
    s7 = ss_mgr.get(collection, 7)
    s8 = ss_mgr.get(collection, 8)

    new_ss = collection.create_snapshot()
    db.Session.add(new_ss)
    db.Session.commit()
    ss_mgr.append(new_ss)

    new_ss = collection.create_snapshot()
    db.Session.add(new_ss)
    db.Session.commit()
    ss_mgr.append(new_ss)

    import time
    time.sleep(0.1)
    print(f'Actives: {ss_mgr.active_snapshots(collection)}')

    for _, seg in seg_commit_mgr.resources[collection.id].items():
        print(f'Commits {seg.id} {seg.refcnt}')
    ss_mgr.release(s7)
    ss_mgr.release(s8)

    # for _, seg in seg_mgr.resources[collection.id].items():
    #     print(f'Segments {seg.id} {seg.refcnt}')

    for _, seg in seg_commit_mgr.resources[collection.id].items():
        print(f'Commits {seg.id} {seg.refcnt}')
