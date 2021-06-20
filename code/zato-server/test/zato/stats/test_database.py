# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import logging
import os
from datetime import datetime
from tempfile import gettempdir
from time import sleep
from unittest import main, TestCase

# dateutil
from dateutil.rrule import SECONDLY, rrule

# Numpy
import numpy as np

# Pandas
import pandas as pd

# Zato
from zato.common.api import Stats
#from zato.common.events.common import EventInfo, PushCtx
from zato.common.test import rand_int, rand_string
#from zato.common.typing_ import asdict, instance_from_dict
from zato.server.connection.connector.subprocess_.impl.events.database import EventsDatabase, OpCode

# ################################################################################################################################
# ################################################################################################################################

if 0:
    from pandas import DataFrame
    from pandas.core.groupby.generic import SeriesGroupBy

    DataFrame = DataFrame
    SeriesGroupBy = SeriesGroupBy

# ################################################################################################################################
# ################################################################################################################################

log_format = '%(asctime)s - %(levelname)s - %(process)d:%(threadName)s - %(name)s:%(lineno)d - %(message)s'
logging.basicConfig(level=logging.WARN, format=log_format)
zato_logger = logging.getLogger('zato')

# ################################################################################################################################
# ################################################################################################################################

utcnow = datetime.utcnow

# ################################################################################################################################
# ################################################################################################################################

class Default:
    LenEvents      = 4
    LenServices    = 3
    IterMultiplier = 11
    SyncThreshold = 100_000_000
    SyncInterval  = 100_000_000

# ################################################################################################################################
# ################################################################################################################################

idx_str_map = {
    1: 'a',
    2: 'b',
    3: 'c',
    4: 'd',
    5: 'e',
    6: 'f',
}

# ################################################################################################################################
# ################################################################################################################################

class ScenarioConfig:
    TimestampFormat = '%Y-%m-%d %H:%M:%S'
    RawStart = '2056-01-02 03:04:00'
    RawEnd   = '2056-01-02 03:05:59'

# ################################################################################################################################
# ################################################################################################################################

class EventsDatabaseTestCase(TestCase):

# ################################################################################################################################

    def yield_scenario_events(self, len_events=None, len_services=None, iter_multiplier=None, events_multiplier=1):

        # This method returns a list of events forming a scenario, with various events
        # belonging to various time buckets. This is unlike yield_raw_events which returns events
        # as they happen, one by one.

        #
        # Our scenario covers two minutes, as configured via ScenarioConfig.
        #
        # For each second within that timeframe we generate len_events for each of the services.
        # How many services there are is configured via len_services.
        #

        start = datetime.strptime(ScenarioConfig.RawStart, ScenarioConfig.TimestampFormat)
        end   = datetime.strptime(ScenarioConfig.RawEnd,   ScenarioConfig.TimestampFormat)

        len_events      = len_events      or Default.LenEvents
        len_services    = len_services    or Default.LenServices
        iter_multiplier = iter_multiplier or Default.IterMultiplier

        for time_bucket in rrule(SECONDLY, dtstart=start, until=end):

            for service_idx in range(1, len_services+1):
                for event_idx in range(1, len_events+1):

                    yield {
                        'timestamp': time_bucket,
                        'object_id': 'service-{}'.format(service_idx),
                        'total_time_ms': service_idx * event_idx * iter_multiplier,
                    }

# ################################################################################################################################

    def yield_scenario_aggr_data(self):
        pass

# ################################################################################################################################

    def yield_raw_events(self, len_events=None, len_services=None, iter_multiplier=None, events_multiplier=1):

        # This method returns a list of raw events, simply as if they were taking
        # place in the system, one by one. This is unlike yield_scenario_events
        # which returns events broken down into specific time buckets, forming a scenario.

        len_events      = len_events      or Default.LenEvents
        len_services    = len_services    or Default.LenServices
        iter_multiplier = iter_multiplier or Default.IterMultiplier

        for service_idx in range(1, len_services+1):

            service_idx_str = str(service_idx)#idx_str_map[service_idx]
            service_name = 'service-{}'.format(service_idx)

            for event_idx in range(1, len_events+1):

                id  = 'id-{}{}'.format(service_idx_str, event_idx)
                cid = 'cid-{}{}'.format(service_idx_str, event_idx)

                '''
                ctx = PushCtx()
                ctx.id = id
                ctx.cid = cid
                ctx.timestamp = utcnow().isoformat()
                ctx.event_type = EventInfo.EventType.service_response
                ctx.object_type = EventInfo.ObjectType.service
                ctx.object_id = service_name
                ctx.total_time_ms = service_idx * event_idx * iter_multiplier
                '''

                ctx = {
                    #'id': id,
                    #'cid': cid,
                    'timestamp': utcnow(),
                    #'event_type': 1_000_001,
                    #'object_type': 2_000_000,
                    'object_id': service_name,
                    'total_time_ms': service_idx * event_idx * iter_multiplier,
                }

                # We are adding a short pause to be better able to observe
                # that each context object has a different timestamp assigned.
                #sleep(0.005)

                yield ctx

# ################################################################################################################################

    def get_random_fs_data_path(self):

        file_name = 'zato-test-events-db-' + rand_string()
        temp_dir = gettempdir()
        fs_data_path = os.path.join(temp_dir, file_name)

        return fs_data_path

# ################################################################################################################################

    def get_events_db(self, logger=None, fs_data_path=None, sync_threshold=None, sync_interval=None, max_retention=None):

        logger         = logger         or zato_logger
        fs_data_path   = fs_data_path   or os.path.join(gettempdir(), rand_string(prefix='fs_data_path'))
        sync_threshold = sync_threshold or Default.SyncThreshold
        sync_interval  = sync_interval  or Default.SyncInterval
        max_retention  = max_retention  or Stats.MaxRetention

        return EventsDatabase(logger, fs_data_path, sync_threshold, sync_interval, max_retention)

# ################################################################################################################################

    def xtest_init(self):

        sync_threshold = rand_int()
        sync_interval  = rand_int()

        events_db = self.get_events_db(sync_threshold=sync_threshold, sync_interval=sync_interval)

        self.assertEqual(events_db.sync_threshold, sync_threshold)
        self.assertEqual(events_db.sync_interval, sync_interval)

# ################################################################################################################################

    def xtest_modify_state_push(self):

        total_events = Default.LenEvents * Default.LenServices

        start = utcnow().isoformat()
        events_db = self.get_events_db()

        for event_data in self.yield_raw_events():
            events_db.modify_state(OpCode.Push, event_data)

        self.assertEqual(len(events_db.in_ram_store), total_events)

        self.assertEqual(events_db.num_events_since_sync, total_events)
        self.assertEqual(events_db.total_events, total_events)

        ctx_list = []

        for item in events_db.in_ram_store:
            ctx = instance_from_dict(PushCtx, item)
            ctx_list.append(ctx)

        self.assertEqual(len(ctx_list), total_events)
        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  0)

        ctx1 = ctx_list[0]   # type: PushCtx
        ctx2 = ctx_list[1]   # type: PushCtx
        ctx3 = ctx_list[2]   # type: PushCtx
        ctx4 = ctx_list[3]   # type: PushCtx

        ctx5 = ctx_list[4]   # type: PushCtx
        ctx6 = ctx_list[5]   # type: PushCtx
        ctx7 = ctx_list[6]   # type: PushCtx
        ctx8 = ctx_list[7]   # type: PushCtx

        ctx9 = ctx_list[8]   # type: PushCtx
        ctx10 = ctx_list[9]  # type: PushCtx
        ctx11 = ctx_list[10] # type: PushCtx
        ctx12 = ctx_list[11] # type: PushCtx

        #
        # ctx.id
        #

        self.assertEqual(ctx1.id,  'id-a1')
        self.assertEqual(ctx2.id,  'id-a2')
        self.assertEqual(ctx3.id,  'id-a3')
        self.assertEqual(ctx4.id,  'id-a4')

        self.assertEqual(ctx5.id,  'id-b1')
        self.assertEqual(ctx6.id,  'id-b2')
        self.assertEqual(ctx7.id,  'id-b3')
        self.assertEqual(ctx8.id,  'id-b4')

        self.assertEqual(ctx9.id,  'id-c1')
        self.assertEqual(ctx10.id, 'id-c2')
        self.assertEqual(ctx11.id, 'id-c3')
        self.assertEqual(ctx12.id, 'id-c4')

        #
        # ctx.cid
        #

        self.assertEqual(ctx1.cid,  'cid-a1')
        self.assertEqual(ctx2.cid,  'cid-a2')
        self.assertEqual(ctx3.cid,  'cid-a3')
        self.assertEqual(ctx4.cid,  'cid-a4')

        self.assertEqual(ctx5.cid,  'cid-b1')
        self.assertEqual(ctx6.cid,  'cid-b2')
        self.assertEqual(ctx7.cid,  'cid-b3')
        self.assertEqual(ctx8.cid,  'cid-b4')

        self.assertEqual(ctx9.cid,  'cid-c1')
        self.assertEqual(ctx10.cid, 'cid-c2')
        self.assertEqual(ctx11.cid, 'cid-c3')
        self.assertEqual(ctx12.cid, 'cid-c4')

        #
        # ctx.timestamp
        #

        self.assertGreater(ctx1.timestamp,  start)
        self.assertGreater(ctx2.timestamp,  ctx1.timestamp)
        self.assertGreater(ctx3.timestamp,  ctx2.timestamp)
        self.assertGreater(ctx4.timestamp,  ctx3.timestamp)

        self.assertGreater(ctx5.timestamp,  ctx4.timestamp)
        self.assertGreater(ctx6.timestamp,  ctx5.timestamp)
        self.assertGreater(ctx7.timestamp,  ctx6.timestamp)
        self.assertGreater(ctx8.timestamp,  ctx7.timestamp)

        self.assertGreater(ctx9.timestamp,  ctx8.timestamp)
        self.assertGreater(ctx10.timestamp, ctx9.timestamp)
        self.assertGreater(ctx11.timestamp, ctx10.timestamp)
        self.assertGreater(ctx12.timestamp, ctx11.timestamp)

        #
        # ctx.event_type
        #

        self.assertEqual(ctx1.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx2.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx3.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx4.event_type,  EventInfo.EventType.service_response)

        self.assertEqual(ctx5.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx6.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx7.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx8.event_type,  EventInfo.EventType.service_response)

        self.assertEqual(ctx9.event_type,  EventInfo.EventType.service_response)
        self.assertEqual(ctx10.event_type, EventInfo.EventType.service_response)
        self.assertEqual(ctx11.event_type, EventInfo.EventType.service_response)
        self.assertEqual(ctx12.event_type, EventInfo.EventType.service_response)

        #
        # ctx.object_type
        #

        self.assertEqual(ctx1.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx2.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx3.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx4.object_type,  EventInfo.ObjectType.service)

        self.assertEqual(ctx5.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx6.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx7.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx8.object_type,  EventInfo.ObjectType.service)

        self.assertEqual(ctx9.object_type,  EventInfo.ObjectType.service)
        self.assertEqual(ctx10.object_type, EventInfo.ObjectType.service)
        self.assertEqual(ctx11.object_type, EventInfo.ObjectType.service)
        self.assertEqual(ctx12.object_type, EventInfo.ObjectType.service)

        #
        # ctx.object_id
        #

        self.assertEqual(ctx1.object_id,  'service-1')
        self.assertEqual(ctx2.object_id,  'service-1')
        self.assertEqual(ctx3.object_id,  'service-1')
        self.assertEqual(ctx4.object_id,  'service-1')

        self.assertEqual(ctx5.object_id,  'service-2')
        self.assertEqual(ctx6.object_id,  'service-2')
        self.assertEqual(ctx7.object_id,  'service-2')
        self.assertEqual(ctx8.object_id,  'service-2')

        self.assertEqual(ctx9.object_id,  'service-3')
        self.assertEqual(ctx10.object_id, 'service-3')
        self.assertEqual(ctx11.object_id, 'service-3')
        self.assertEqual(ctx12.object_id, 'service-3')

        #
        # ctx.total_time_ms
        #

        self.assertEqual(ctx1.total_time_ms, 11)
        self.assertEqual(ctx2.total_time_ms, 22)
        self.assertEqual(ctx3.total_time_ms, 33)
        self.assertEqual(ctx4.total_time_ms, 44)

        self.assertEqual(ctx5.total_time_ms, 22)
        self.assertEqual(ctx6.total_time_ms, 44)
        self.assertEqual(ctx7.total_time_ms, 66)
        self.assertEqual(ctx8.total_time_ms, 88)

        self.assertEqual(ctx9.total_time_ms, 33)
        self.assertEqual(ctx10.total_time_ms, 66)
        self.assertEqual(ctx11.total_time_ms, 99)
        self.assertEqual(ctx12.total_time_ms, 132)

# ################################################################################################################################

    def xtest_get_data_from_ram(self):

        start = utcnow().isoformat()
        events_db = self.get_events_db()

        for event_data in self.yield_raw_events():
            events_db.modify_state(OpCode.Push, event_data)

        data = events_db.get_data_from_ram()

        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  0)

        #
        # data.id
        #

        data_id = data['id']

        self.assertEqual(data_id[0],  'id-a1')
        self.assertEqual(data_id[1],  'id-a2')
        self.assertEqual(data_id[2],  'id-a3')
        self.assertEqual(data_id[3],  'id-a4')

        self.assertEqual(data_id[4],  'id-b1')
        self.assertEqual(data_id[5],  'id-b2')
        self.assertEqual(data_id[6],  'id-b3')
        self.assertEqual(data_id[7],  'id-b4')

        self.assertEqual(data_id[8],  'id-c1')
        self.assertEqual(data_id[9],  'id-c2')
        self.assertEqual(data_id[10], 'id-c3')
        self.assertEqual(data_id[11], 'id-c4')

        #
        # data.cid
        #

        data_cid = data['cid']

        self.assertEqual(data_cid[0],  'cid-a1')
        self.assertEqual(data_cid[1],  'cid-a2')
        self.assertEqual(data_cid[2],  'cid-a3')
        self.assertEqual(data_cid[3],  'cid-a4')

        self.assertEqual(data_cid[4],  'cid-b1')
        self.assertEqual(data_cid[5],  'cid-b2')
        self.assertEqual(data_cid[6],  'cid-b3')
        self.assertEqual(data_cid[7],  'cid-b4')

        self.assertEqual(data_cid[8],  'cid-c1')
        self.assertEqual(data_cid[9],  'cid-c2')
        self.assertEqual(data_cid[10], 'cid-c3')
        self.assertEqual(data_cid[11], 'cid-c4')

        #
        # data.timestamp
        #

        data_timestamp = data['timestamp']

        self.assertGreater(data_timestamp[0],  start)
        self.assertGreater(data_timestamp[1],  data_timestamp[0])
        self.assertGreater(data_timestamp[2],  data_timestamp[1])
        self.assertGreater(data_timestamp[3],  data_timestamp[2])

        self.assertGreater(data_timestamp[4],  data_timestamp[3])
        self.assertGreater(data_timestamp[5],  data_timestamp[4])
        self.assertGreater(data_timestamp[6],  data_timestamp[5])
        self.assertGreater(data_timestamp[7],  data_timestamp[6])

        self.assertGreater(data_timestamp[8],  data_timestamp[7])
        self.assertGreater(data_timestamp[9],  data_timestamp[8])
        self.assertGreater(data_timestamp[10],  data_timestamp[9])
        self.assertGreater(data_timestamp[11],  data_timestamp[10])

        #
        # data.event_type
        #

        data_event_type = data['event_type']

        self.assertEqual(data_event_type[0],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[1],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[2],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[3],  EventInfo.EventType.service_response)

        self.assertEqual(data_event_type[4],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[5],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[6],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[7],  EventInfo.EventType.service_response)

        self.assertEqual(data_event_type[8],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[9],  EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[10], EventInfo.EventType.service_response)
        self.assertEqual(data_event_type[11], EventInfo.EventType.service_response)

        #
        # ctx.object_type
        #

        data_object_type = data['object_type']

        self.assertEqual(data_object_type[0],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[1],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[2],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[3],  EventInfo.ObjectType.service)

        self.assertEqual(data_object_type[4],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[5],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[6],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[7],  EventInfo.ObjectType.service)

        self.assertEqual(data_object_type[8],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[9],  EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[10], EventInfo.ObjectType.service)
        self.assertEqual(data_object_type[11], EventInfo.ObjectType.service)

        #
        # ctx.object_id
        #

        data_object_id = data['object_id']

        self.assertEqual(data_object_id[0],  'service-1')
        self.assertEqual(data_object_id[1],  'service-1')
        self.assertEqual(data_object_id[2],  'service-1')
        self.assertEqual(data_object_id[3],  'service-1')

        self.assertEqual(data_object_id[4],  'service-2')
        self.assertEqual(data_object_id[5],  'service-2')
        self.assertEqual(data_object_id[6],  'service-2')
        self.assertEqual(data_object_id[7],  'service-2')

        self.assertEqual(data_object_id[8],  'service-3')
        self.assertEqual(data_object_id[9],  'service-3')
        self.assertEqual(data_object_id[10], 'service-3')
        self.assertEqual(data_object_id[11], 'service-3')

        #
        # ctx.total_time_ms
        #

        data_total_time_ms = data['total_time_ms']

        self.assertEqual(data_total_time_ms[0], 11)
        self.assertEqual(data_total_time_ms[1], 22)
        self.assertEqual(data_total_time_ms[2], 33)
        self.assertEqual(data_total_time_ms[3], 44)

        self.assertEqual(data_total_time_ms[4], 22)
        self.assertEqual(data_total_time_ms[5], 44)
        self.assertEqual(data_total_time_ms[6], 66)
        self.assertEqual(data_total_time_ms[7], 88)

        self.assertEqual(data_total_time_ms[8], 33)
        self.assertEqual(data_total_time_ms[9], 66)
        self.assertEqual(data_total_time_ms[10], 99)
        self.assertEqual(data_total_time_ms[11], 132)

# ################################################################################################################################

    def xtest_get_data_from_storage_path_does_not_exist(self):

        # Be explicit about the fact that we are using a random path, one that does not exist
        fs_data_path = rand_string()

        # Create a new instance
        events_db = self.get_events_db(fs_data_path=fs_data_path)

        # This should return an empty DataFrame because the path did not exist
        data = events_db.get_data_from_storage() # type: DataFrame

        self.assertEqual(len(data), 0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  0)

# ################################################################################################################################

    def xtest_get_data_from_storage_path_exists(self):

        # This is where we keep Parquet data
        fs_data_path = self.get_random_fs_data_path()

        # Obtain test data
        test_data = list(self.yield_raw_events())

        # Turn it into a DataFrame
        data_frame = pd.DataFrame(test_data)

        # Save it as as a Parquet file
        data_frame.to_parquet(fs_data_path)

        # Create a new DB instance
        events_db = self.get_events_db(fs_data_path=fs_data_path)

        # This should return an empty DataFrame because the path did not exist
        data = events_db.get_data_from_storage() # type: DataFrame

        self.assertEqual(len(data), len(test_data))
        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  1)

# ################################################################################################################################

    def xtest_sync_state(self):

        # This is where we keep Parquet data
        fs_data_path = self.get_random_fs_data_path()

        # Obtain test data
        test_data = list(self.yield_raw_events())

        # Turn it into a DataFrame
        data_frame = pd.DataFrame(test_data)

        # Save it as as a Parquet file
        data_frame.to_parquet(fs_data_path)

        # Create a new test DB instance ..
        events_db = self.get_events_db(fs_data_path=fs_data_path)

        # Push data to RAM ..
        for event_data in self.yield_raw_events():
            events_db.modify_state(OpCode.Push, event_data)

        # At this point, we should have data on disk and in RAM
        # and syncing should push data from RAM to disk.
        events_db.sync_state()

        # This should data from what was previously in RAM combined with what was on disk
        data = events_db.get_data_from_storage() # type: DataFrame

        # The length should be equal to twice the defaults - it is twice
        # because we generated test data two times, once for Parquet and once when it was added to RAM
        self.assertTrue(len(data), 2 * Default.LenEvents * Default.LenServices)

        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 0)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  2)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CombineData], 1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.SaveData],    1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.SyncState],   1)

# ################################################################################################################################

    def xtest_sync_threshold(self):

        num_iters = 3
        sync_threshold = 1

        events_db = self.get_events_db(sync_threshold=sync_threshold)

        for x in range(num_iters):
            events_db.modify_state(OpCode.Push, {'timestamp':'unused'})

        # This is 0 because we were syncing state after each modification
        self.assertEqual(events_db.num_events_since_sync, 0)

        self.assertEqual(events_db.telemetry[OpCode.Internal.SaveData],    3)
        self.assertEqual(events_db.telemetry[OpCode.Internal.SyncState],   3)
        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  3)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  2)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CombineData], 3)

# ################################################################################################################################

    def xtest_sync_interval(self):

        num_iters = 3
        sync_interval = 0.001

        events_db = self.get_events_db(sync_interval=sync_interval)

        for x in range(num_iters):
            events_db.modify_state(OpCode.Push, {'timestamp':'unused'})
            sleep(sync_interval * 5)

        # This is 0 because we were syncing state after each modification
        self.assertEqual(events_db.num_events_since_sync, 0)

        self.assertEqual(events_db.telemetry[OpCode.Internal.SaveData],    3)
        self.assertEqual(events_db.telemetry[OpCode.Internal.SyncState],   3)
        self.assertEqual(events_db.telemetry[OpCode.Internal.GetFromRAM],  3)
        self.assertEqual(events_db.telemetry[OpCode.Internal.ReadParqet],  2)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CreateNewDF], 1)
        self.assertEqual(events_db.telemetry[OpCode.Internal.CombineData], 3)

# ################################################################################################################################

    def xtest_max_retention(self):

        # Synchronise after each push
        sync_threshold=1

        # This is in milliseconds
        max_retention = 200

        max_retension_sec = max_retention / 1000.0
        sleep_time = max_retension_sec + (max_retension_sec * 0.1)

        # This is where we keep Parquet data
        fs_data_path = self.get_random_fs_data_path()

        # Create a new DB instance upfront
        events_db = self.get_events_db(fs_data_path=fs_data_path, sync_threshold=sync_threshold, max_retention=max_retention)

        # Get events ..
        event_data_list = list(self.yield_raw_events(len_events=3, len_services=1))
        event_data1 = event_data_list[0] # type: PushCtx
        event_data2 = event_data_list[1] # type: PushCtx
        event_data3 = event_data_list[2] # type: PushCtx

        # First call, set its timestamp and push the event
        event_data1['timestamp'] = utcnow().isoformat()
        events_db.modify_state(OpCode.Push, event_data1)

        # Sleep longer than retention time
        sleep(sleep_time)

        # Second call, set its timestamp too and push the event
        event_data2['timestamp'] = utcnow().isoformat()

        # Again, longer than retentiom time
        sleep(sleep_time)

        # The last call - there is no sleep afterwards, only push, which, given that the retention time is big enough,
        # means that it should be the only event left around in the storage.
        # Note that we assume that our max_retention will be enough for this push to succeed.
        event_data3['timestamp'] = utcnow().isoformat()
        events_db.modify_state(OpCode.Push, event_data3)

        # Read the state from persistent storage ..

        data = events_db.get_data_from_storage()

        # .. only the last push should be available ..
        self.assertEqual(len(data), 1)

        # .. convert to a form that is easier to test ..
        data = data.transpose()
        data = data[0]

        # .. run all the remaining assertions now.
        self.assertEqual(data['id'],            event_data3['id'])
        self.assertEqual(data['cid'],           event_data3['cid'])
        self.assertEqual(data['timestamp'],     event_data3['timestamp'])
        self.assertEqual(data['event_type'],    event_data3['event_type'])
        self.assertEqual(data['object_type'],   event_data3['object_type'])
        self.assertEqual(data['object_id'],     event_data3['object_id'])
        self.assertEqual(data['total_time_ms'], event_data3['total_time_ms'])

        self.assertIs(data['source_type'],    event_data3['source_type'])
        self.assertIs(data['source_id'],      event_data3['source_id'])
        self.assertIs(data['recipient_type'], event_data3['recipient_type'])
        self.assertIs(data['recipient_id'],   event_data3['recipient_id'])

# ################################################################################################################################

    def impl_get_events_by_response_time(self, data, count=10, time_label=None, min_time=None, max_time=None,
        time_freq='5m'):
        # type: (DataFrame, int, str, str, str) -> list

        # time_label -> e.g. today, yesterday, this_year, last_week

        #data = data[['object_id', 'total_time_ms']].groupby(['total_time_ms']).mean()

        #data = data.set_index(pd.DatetimeIndex(data['timestamp']))
        #data.index.name = 'idx_timestamp'


        print(len(data))

        start = utcnow()

        print('QQQ-1', start)
        #print()

        #print('RRR-1', data['total_time_ms'].sum())
        print()

        #print('RRR-2', data.groupby('object_id').count()['timestamp'].to_dict())
        #print()

        #by_object_id = data.groupby('object_id')

        aggregated = data.groupby([
            pd.Grouper(key='timestamp', freq=time_freq),
            pd.Grouper(key='object_id'),
        ]).agg(**{
            'item_max':  pd.NamedAgg(column='total_time_ms', aggfunc=np.max),
            'item_min':  pd.NamedAgg(column='total_time_ms', aggfunc=np.min),
            'item_sum':  pd.NamedAgg(column='total_time_ms', aggfunc=np.sum),
            'item_mean': pd.NamedAgg(column='total_time_ms', aggfunc=np.mean),
        })

        print('QQQ-2', utcnow() - start)

        #print(pd.concat([aggregated] * 10))

        '''
        aggr_response_time = aggregated['total_time_ms'] # type: SeriesGroupBy

        print('QQQ-3', utcnow() - start)

        pd.DataFrame(aggr_response_time.max()).to_parquet('/tmp/zzz-1')

        print('QQQ-4', utcnow() - start)

        pd.DataFrame(aggr_response_time.min()).to_parquet('/tmp/zzz-2')

        print('QQQ-5', utcnow() - start)

        pd.DataFrame(aggr_response_time.sum()).to_parquet('/tmp/zzz-3')

        print('QQQ-6', utcnow() - start)

        pd.DataFrame(aggr_response_time.mean()).to_parquet('/tmp/zzz-4')

        print('QQQ-7', utcnow() - start)

        #df = pd.DataFrame(aggr_response_time.max())
        #print(df.to_parquet('/tmp/zzz'))

        #print(aggr_response_time.min())

        #all_total_time_ms = data.groupby('object_id')['total_time_ms'] # type: DataFrame

        #all_total_time_ms_summed = all_total_time_ms.sum()
        #all_total_time_ms_mean   = all_total_time_ms.mean()
        '''

        #print('QQQ-Z', utcnow() - start)
        #print()

        #print(all_total_time_ms_summed)

        #print(222, all_total_time_ms_mean.to_dict())

# ################################################################################################################################

    def test_get_events_by_response_time(self):

        # Generate test events ..
        data = self.yield_scenario_events()
        data = pd.DataFrame(data)

        # .. create a new DB instance ..
        events_db = self.get_events_db()

        # .. aggregate test events ..
        aggregated = events_db.aggregate(data)

        # .. convert pd.Timestamp objects to string for easy testing ..
        for row in aggregated:
            #row['timestamp'] = row['timestamp'].to_pydatetime()
            print(111, row)

        # .. convert it to a dict to make it easier to construct assertions ..
        aggregated = aggregated.to_dict()

        # .. and run all the asssertions now.

        print(aggregated)

        #for item in events:
        #    print(item)

        #print(111, len(events))

        #data = pd.DataFrame(data)
        #result = self.impl_get_events_by_response_time(data)

# ################################################################################################################################

if __name__ == '__main__':
    main()

# ################################################################################################################################
