# -*- coding: utf-8 -*-

"""
Copyright (C) 2021, Zato Source s.r.o. https://zato.io

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

# stdlib
import logging
import os
from datetime import datetime
from logging import DEBUG, INFO, WARN
from platform import system as platform_system
from random import seed as random_seed
from tempfile import mkstemp
from traceback import format_exc
from uuid import uuid4

# gevent
import gevent.monkey # Needed for Cassandra

# Paste
from paste.util.converters import asbool

# pysimdjson
from simdjson import Parser as SIMDJSONParser

# Zato
from zato.broker import BrokerMessageReceiver
from zato.broker.client import BrokerClient
from zato.bunch import Bunch
from zato.common.api import DATA_FORMAT, default_internal_modules, HotDeploy, KVDB, RATE_LIMIT, SERVER_STARTUP, \
    SERVER_UP_STATUS, ZatoKVDB as CommonZatoKVDB, ZATO_ODB_POOL_NAME
from zato.common.audit import audit_pii
from zato.common.audit_log import AuditLog
from zato.common.broker_message import HOT_DEPLOY, MESSAGE_TYPE
from zato.common.const import SECRETS
from zato.common.events.common import Default as EventsDefault
from zato.common.ipc.api import IPCAPI
from zato.common.json_internal import dumps, loads
from zato.common.kv_data import KVDataAPI
from zato.common.marshal_.api import MarshalAPI
from zato.common.odb.post_process import ODBPostProcess
from zato.common.pubsub import SkipDelivery
from zato.common.rate_limiting import RateLimiting
from zato.common.util.api import absolutize, get_config, get_kvdb_config_for_log, get_user_config_name, hot_deploy, \
     invoke_startup_services as _invoke_startup_services, new_cid, spawn_greenlet, StaticConfig, \
     register_diag_handlers
from zato.common.util.platform_ import is_posix
from zato.common.util.posix_ipc_ import ConnectorConfigIPC, ServerStartupIPC
from zato.common.util.time_ import TimeUtil
from zato.common.util.tcp import wait_until_port_taken
from zato.distlock import LockManager
from zato.server.base.worker import WorkerStore
from zato.server.config import ConfigStore
from zato.server.connection.stats import ServiceStatsClient
from zato.server.connection.server.rpc.api import ConfigCtx as _ServerRPC_ConfigCtx, ServerRPC
from zato.server.connection.server.rpc.config import ODBConfigSource
from zato.server.connection.kvdb.api import KVDB as ZatoKVDB
from zato.server.base.parallel.config import ConfigLoader
from zato.server.base.parallel.http import HTTPHandler
from zato.server.base.parallel.subprocess_.api import CurrentState as SubprocessCurrentState, \
     StartConfig as SubprocessStartConfig
from zato.server.base.parallel.subprocess_.ftp import FTPIPC
from zato.server.base.parallel.subprocess_.ibm_mq import IBMMQIPC
from zato.server.base.parallel.subprocess_.zato_events import ZatoEventsIPC
from zato.server.base.parallel.subprocess_.outconn_sftp import SFTPIPC
from zato.server.sso import SSOTool

# ################################################################################################################################

# Python 2/3 compatibility
from past.builtins import unicode

# ################################################################################################################################

if 0:

    # Zato
    from zato.common.crypto.api import ServerCryptoManager
    from zato.common.odb.api import ODBManager
    from zato.common.odb.model import Cluster as ClusterModel
    from zato.common.typing_ import any_
    from zato.server.connection.connector.subprocess_.ipc import SubprocessIPC
    from zato.server.service.store import ServiceStore
    from zato.simpleio import SIOServerConfig
    from zato.server.startup_callable import StartupCallableTool
    from zato.sso.api import SSOAPI

    # For pyflakes
    ODBManager = ODBManager
    ServerCryptoManager = ServerCryptoManager
    ServiceStore = ServiceStore
    SIOServerConfig = SIOServerConfig
    SSOAPI = SSOAPI
    StartupCallableTool = StartupCallableTool
    SubprocessIPC = SubprocessIPC

# ################################################################################################################################

logger = logging.getLogger(__name__)
kvdb_logger = logging.getLogger('zato_kvdb')

# ################################################################################################################################

megabyte = 10**6

# ################################################################################################################################
# ################################################################################################################################

class ParallelServer(BrokerMessageReceiver, ConfigLoader, HTTPHandler):
    """ Main server process.
    """
    def __init__(self):
        self.logger = logger
        self.host = None
        self.port = None
        self.is_starting_first = '<not-set>'
        self.crypto_manager = None # type: ServerCryptoManager
        self.odb = None # type: ODBManager
        self.odb_data = None
        self.config = None # type: ConfigStore
        self.repo_location = None
        self.user_conf_location = None
        self.sql_pool_store = None
        self.soap11_content_type = None
        self.soap12_content_type = None
        self.plain_xml_content_type = None
        self.json_content_type = None
        self.service_modules = None   # Set programmatically in Spring
        self.service_sources = []   # Set in a config file
        self.base_dir = None          # type: unicode
        self.tls_dir = None           # type: unicode
        self.static_dir = None        # type: unicode
        self.json_schema_dir = None   # type: unicode
        self.sftp_channel_dir = None  # type: unicode
        self.hot_deploy_config = None # type: Bunch
        self.fs_server_config = None # type: any_
        self.fs_sql_config = None # type: Bunch
        self.pickup_config = None # type: Bunch
        self.logging_config = None # type: Bunch
        self.logging_conf_path = None # type: unicode
        self.sio_config = None # type: SIOServerConfig
        self.sso_config = None
        self.connector_server_grace_time = None
        self.id = 0    # type: int
        self.name = '' # type: str
        self.worker_id = None # type: int
        self.worker_pid = None # type: int
        self.cluster = None # type: ClusterModel
        self.cluster_id = None # type: int
        self.cluster_name = None # type: str
        self.kvdb = None # type: KVDB
        self.startup_jobs = None # type: dict
        self.worker_store = None # type: WorkerStore
        self.service_store = None # type: ServiceStore
        self.request_dispatcher_dispatch = None
        self.deployment_lock_expires = None # type: int
        self.deployment_lock_timeout = None # type: int
        self.deployment_key = ''
        self.has_gevent = None # type: bool
        self.delivery_store = None
        self.static_config = None # type: Bunch()
        self.component_enabled = Bunch()
        self.client_address_headers = ['HTTP_X_ZATO_FORWARDED_FOR', 'HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR']
        self.broker_client = None # type: BrokerClient
        self.return_tracebacks = None # type: bool
        self.default_error_message = None # type: unicode
        self.time_util = TimeUtil()
        self.preferred_address = None # type: unicode
        self.crypto_use_tls = None # type: bool
        self.rpc = None # type: ServerRPC
        self.zato_lock_manager = None # type: LockManager
        self.pid = None # type: int
        self.sync_internal = None # type: bool
        self.ipc_api = IPCAPI()
        self.fifo_response_buffer_size = None # type: int # Will be in megabytes
        self.is_first_worker = None # type: bool
        self.shmem_size = -1.0
        self.server_startup_ipc = ServerStartupIPC()
        self.connector_config_ipc = ConnectorConfigIPC()
        self.sso_api = None # type: SSOAPI
        self.is_sso_enabled = False
        self.audit_pii = audit_pii
        self.has_fg = False
        self.startup_callable_tool = None # type: StartupCallableTool
        self.default_internal_pubsub_endpoint_id = None
        self.rate_limiting = None # type: RateLimiting
        self.jwt_secret = None # type: bytes
        self._hash_secret_method = None # type: unicode
        self._hash_secret_rounds = None # type: int
        self._hash_secret_salt_size = None # type: int
        self.sso_tool = SSOTool(self)
        self.platform_system = platform_system().lower() # type: unicode
        self.has_posix_ipc = is_posix
        self.user_config = Bunch()
        self.stderr_path = None # type: str
        self.json_parser = SIMDJSONParser()
        self.work_dir = 'ParallelServer-work_dir'
        self.events_dir = 'ParallelServer-events_dir'
        self.kvdb_dir = 'ParallelServer-kvdb_dir'
        self.marshal_api = MarshalAPI()

        # SQL-based key/value data
        self.kv_data_api = None # type: KVDataAPI

        # Transient API for in-RAM messages
        self.zato_kvdb = ZatoKVDB()

        # In-RAM statistics
        self.slow_responses = self.zato_kvdb.internal_create_list_repo(CommonZatoKVDB.SlowResponsesName)
        self.usage_samples = self.zato_kvdb.internal_create_list_repo(CommonZatoKVDB.UsageSamplesName)
        self.current_usage = self.zato_kvdb.internal_create_number_repo(CommonZatoKVDB.CurrentUsageName)
        self.pub_sub_metadata = self.zato_kvdb.internal_create_object_repo(CommonZatoKVDB.PubSubMetadataName)

        self.stats_client = ServiceStatsClient()
        self._stats_host = '<ParallelServer-_stats_host>'
        self._stats_port = -1

        # Audit log
        self.audit_log = AuditLog()

        # Current state of subprocess-based connectors
        self.subproc_current_state = SubprocessCurrentState()

        # Our arbiter may potentially call the cleanup procedure multiple times
        # and this will be set to True the first time around.
        self._is_process_closing = False

        # Internal caches - not to be used by user services
        self.internal_cache_patterns = {}
        self.internal_cache_lock_patterns = gevent.lock.RLock()

        # Allows users store arbitrary data across service invocations
        self.user_ctx = Bunch()
        self.user_ctx_lock = gevent.lock.RLock()

        # Connectors
        self.connector_ftp    = FTPIPC(self)
        self.connector_ibm_mq = IBMMQIPC(self)
        self.connector_sftp   = SFTPIPC(self)
        self.connector_events = ZatoEventsIPC(self)

        # HTTP methods allowed as a Python list
        self.http_methods_allowed = []

        # As above, but as a regular expression pattern
        self.http_methods_allowed_re = ''

        self.access_logger = logging.getLogger('zato_access_log')
        self.access_logger_log = self.access_logger._log
        self.needs_access_log = self.access_logger.isEnabledFor(INFO)
        self.needs_all_access_log = True
        self.access_log_ignore = set()
        self.has_pubsub_audit_log = logging.getLogger('zato_pubsub_audit').isEnabledFor(DEBUG)
        self.is_enabled_for_warn = logging.getLogger('zato').isEnabledFor(WARN)
        self.is_admin_enabled_for_info = logging.getLogger('zato_admin').isEnabledFor(INFO)

        # The main config store
        self.config = ConfigStore()

# ################################################################################################################################

    def deploy_missing_services(self, locally_deployed):
        """ Deploys services that exist on other servers but not on ours.
        """
        # The locally_deployed list are all the services that we could import based on our current
        # understanding of the contents of the cluster. However, it's possible that we have
        # been shut down for a long time and during that time other servers deployed services
        # we don't know anything about. They are not stored locally because we were down.
        # Hence we need to check out if there are any other servers in the cluster and if so,
        # grab their list of services, compare it with what we have deployed and deploy
        # any that are missing.

        # Continue only if there is more than one running server in the cluster.
        other_servers = self.odb.get_servers()

        if other_servers:
            other_server = other_servers[0] # Index 0 is as random as any other because the list is not sorted.
            missing = self.odb.get_missing_services(other_server, {item.name for item in locally_deployed})

            if missing:

                logger.info('Found extra services to deploy: %s', ', '.join(sorted(item.name for item in missing)))

                # (file_name, source_path) -> a list of services it contains
                modules = {}

                # Coalesce all service modules - it is possible that each one has multiple services
                # so we do want to deploy the same module over for each service found.
                for _ignored_service_id, name, source_path, source in missing:
                    file_name = os.path.basename(source_path)
                    _, tmp_full_path = mkstemp(suffix='-'+ file_name)

                    # Module names are unique so they can serve as keys
                    key = file_name

                    if key not in modules:
                        modules[key] = {
                            'tmp_full_path': tmp_full_path,
                            'services': [name] # We can append initial name already in this 'if' branch
                        }

                        # Save the source code only once here
                        f = open(tmp_full_path, 'wb')
                        f.write(source)
                        f.close()

                    else:
                        modules[key]['services'].append(name)

                # Create a deployment package in ODB out of which all the services will be picked up ..
                for file_name, values in modules.items():
                    msg = Bunch()
                    msg.action = HOT_DEPLOY.CREATE_SERVICE.value
                    msg.msg_type = MESSAGE_TYPE.TO_PARALLEL_ALL
                    msg.package_id = hot_deploy(self, file_name, values['tmp_full_path'], notify=False)

                    # .. and tell the worker to actually deploy all the services the package contains.
                    # gevent.spawn(self.worker_store.on_broker_msg_HOT_DEPLOY_CREATE_SERVICE, msg)
                    self.worker_store.on_broker_msg_HOT_DEPLOY_CREATE_SERVICE(msg)

                    logger.info('Deployed extra services found: %s', sorted(values['services']))

# ################################################################################################################################

    def maybe_on_first_worker(self, server):
        """ This method will execute code with a distibuted lock held. We need a lock because we can have multiple worker
        processes fighting over the right to redeploy services. The first worker to obtain the lock will actually perform
        the redeployment and set a flag meaning that for this particular deployment key (and remember that each server restart
        means a new deployment key) the services have been already deployed. Further workers will check that the flag exists
        and will skip the deployment altogether.
        """
        def import_initial_services_jobs():

            # All non-internal services that we have deployed
            locally_deployed = []

            # Internal modules with that are potentially to be deployed
            internal_service_modules = []

            # This was added between 3.0 and 3.1, which is why it is optional
            deploy_internal = self.fs_server_config.get('deploy_internal', default_internal_modules)

            # Above, we potentially got the list of internal modules to be deployed as they were defined in server.conf.
            # However, if someone creates an environment and then we add a new module, this module will not neccessarily
            # exist in server.conf. This is why we need to add any such missing ones explicitly below.
            for internal_module, is_enabled in default_internal_modules.items():
                if internal_module not in deploy_internal:
                    deploy_internal[internal_module] = is_enabled

            # All internal modules were found, now we can build a list of what is to be enabled.
            for module_name, is_enabled in deploy_internal.items():
                if is_enabled:
                    internal_service_modules.append(module_name)

            locally_deployed.extend(self.service_store.import_internal_services(
                internal_service_modules, self.base_dir, self.sync_internal, self.is_starting_first))

            logger.info('Deploying user-defined services (%s)', self.name)

            user_defined_deployed = self.service_store.import_services_from_anywhere(
                self.service_modules + self.service_sources, self.base_dir).to_process

            locally_deployed.extend(user_defined_deployed)
            len_user_defined_deployed = len(user_defined_deployed)

            suffix = ' ' if len_user_defined_deployed == 1 else 's '

            logger.info('Deployed %d user-defined service%s (%s)', len_user_defined_deployed, suffix, self.name)

            return set(locally_deployed)

        lock_name = '{}{}:{}'.format(KVDB.LOCK_SERVER_STARTING, self.fs_server_config.main.token, self.deployment_key)
        already_deployed_flag = '{}{}:{}'.format(KVDB.LOCK_SERVER_ALREADY_DEPLOYED,
                                                 self.fs_server_config.main.token, self.deployment_key)

        logger.debug('Will use the lock_name: `%s`', lock_name)

        with self.zato_lock_manager(lock_name, ttl=self.deployment_lock_expires, block=self.deployment_lock_timeout):
            if self.kv_data_api.get(already_deployed_flag):
                # There has been already the first worker who's done everything there is to be done so we may just return.
                self.is_starting_first = False
                logger.debug('Not attempting to obtain the lock_name:`%s`', lock_name)

                # Simply deploy services, including any missing ones, the first worker has already cleared out the ODB
                locally_deployed = import_initial_services_jobs()

                return locally_deployed

            else:
                # We are this server's first worker so we need to re-populate
                # the database and create the flag indicating we're done.
                self.is_starting_first = True
                logger.debug('Got lock_name:`%s`, ttl:`%s`', lock_name, self.deployment_lock_expires)

                # .. Remove all the deployed services from the DB ..
                self.odb.drop_deployed_services(server.id)

                # .. deploy them back including any missing ones found on other servers.
                locally_deployed = import_initial_services_jobs()

                # Add the flag to Redis indicating that this server has already
                # deployed its services. Note that by default the expiration
                # time is more than a century in the future. It will be cleared out
                # next time the server will be started.

                self.kv_data_api.set(
                    already_deployed_flag,
                    dumps({'create_time_utc':datetime.utcnow().isoformat()}),
                    self.deployment_lock_expires,
                )

                return locally_deployed

# ################################################################################################################################

    def get_full_name(self):
        """ Returns this server's full name in the form of server@cluster.
        """
        return '{}@{}'.format(self.name, self.cluster_name)

# ################################################################################################################################

    def _after_init_common(self, server):
        """ Initializes parts of the server that don't depend on whether the server's been allowed to join the cluster or not.
        """
        def _normalise_service_source_path(name:str) -> str:
            if not os.path.isabs(name):
                name = os.path.normpath(os.path.join(self.base_dir, name))
            return name

        # Patterns to match during deployment
        self.service_store.patterns_matcher.read_config(self.fs_server_config.deploy_patterns_allowed)

        # Static config files
        self.static_config = StaticConfig(self.static_dir)

        # SSO e-mail templates
        self.static_config.read_directory(os.path.join(self.static_dir, 'sso', 'email'))

        # Key-value DB
        kvdb_config = get_kvdb_config_for_log(self.fs_server_config.kvdb)
        kvdb_logger.info('Worker config `%s`', kvdb_config)

        self.kvdb.config = self.fs_server_config.kvdb
        self.kvdb.server = self
        self.kvdb.decrypt_func = self.crypto_manager.decrypt

        kvdb_logger.info('Worker config `%s`', kvdb_config)

        if self.fs_server_config.kvdb.host:
            self.kvdb.init()

        # New in 3.1, it may be missing in the config file
        if not self.fs_server_config.misc.get('sftp_genkey_command'):
            self.fs_server_config.misc.sftp_genkey_command = 'dropbearkey'

        # New in 3.2, may be missing in the config file
        allow_internal = self.fs_server_config.misc.get('service_invoker_allow_internal', [])
        allow_internal = allow_internal if isinstance(allow_internal, list) else [allow_internal]
        self.fs_server_config.misc.service_invoker_allow_internal = allow_internal

        # Service sources from server.conf
        for name in open(os.path.join(self.repo_location, self.fs_server_config.main.service_sources)):
            name = name.strip()
            if name and not name.startswith('#'):
                name = _normalise_service_source_path(name)
                self.service_sources.append(name)

        # Service sources from user-defined hot-deployment configuration
        for key, value in self.pickup_config.items():
            if key.startswith(HotDeploy.UserPrefix):
                pickup_from = value.get('pickup_from')
                if pickup_from:
                    pickup_from = _normalise_service_source_path(pickup_from)
                    self.service_sources.append(pickup_from)

        # User-config from ./config/repo/user-config
        for file_name in os.listdir(self.user_conf_location):
            conf = get_config(self.user_conf_location, file_name)

            # Not used at all in this type of configuration
            conf.pop('user_config_items', None)

            self.user_config[get_user_config_name(file_name)] = conf

        # Convert size of FIFO response buffers to megabytes
        self.fifo_response_buffer_size = int(float(self.fs_server_config.misc.fifo_response_buffer_size) * megabyte)

        locally_deployed = self.maybe_on_first_worker(server)

        return locally_deployed

# ################################################################################################################################

    def set_up_odb(self):
        # This is the call that creates an SQLAlchemy connection
        self.config.odb_data['fs_sql_config'] = self.fs_sql_config
        self.sql_pool_store[ZATO_ODB_POOL_NAME] = self.config.odb_data
        self.odb.pool = self.sql_pool_store[ZATO_ODB_POOL_NAME].pool
        self.odb.token = self.config.odb_data.token.decode('utf8')
        self.odb.decrypt_func = self.decrypt

# ################################################################################################################################

    def build_server_rpc(self):

        # What our configuration backend is
        config_source = ODBConfigSource(self.odb, self.cluster_name, self.name, self.decrypt)

        # A combination of backend and runtime configuration
        config_ctx = _ServerRPC_ConfigCtx(config_source, self)

        # A publicly available RPC client
        return ServerRPC(config_ctx)

# ################################################################################################################################

    def _run_stats_client(self, events_tcp_port):
        # type: (int) -> None
        self.stats_client.init('127.0.0.1', events_tcp_port)
        self.stats_client.run()

# ################################################################################################################################

    @staticmethod
    def start_server(parallel_server, zato_deployment_key=None):

        # Easier to type
        self = parallel_server # type: ParallelServer

        # This cannot be done in __init__ because each sub-process obviously has its own PID
        self.pid = os.getpid()

        # This also cannot be done in __init__ which doesn't have this variable yet
        self.is_first_worker = int(os.environ['ZATO_SERVER_WORKER_IDX']) == 0

        # Used later on
        use_tls = asbool(self.fs_server_config.crypto.use_tls)

        # This changed in 3.2 so we need to take both into account
        self.work_dir = self.fs_server_config.main.get('work_dir') or self.fs_server_config.hot_deploy.get('work_dir')
        self.work_dir = os.path.normpath(os.path.join(self.repo_location, self.work_dir))

        # Make sure the directories for events exists
        events_dir_v1 = os.path.join(self.work_dir, 'events', 'v1')

        for name in 'v1', 'v2':
            full_path = os.path.join(self.work_dir, 'events', name)
            if not os.path.exists(full_path):
                os.makedirs(full_path, mode=0o770, exist_ok=True)

        # Set for later use - this is the version that we currently employ and we know that it exists.
        self.events_dir = events_dir_v1

        # Will be None if we are not running in background.
        if not zato_deployment_key:
            zato_deployment_key = '{}.{}'.format(datetime.utcnow().isoformat(), uuid4().hex)

        # Each time a server starts a new deployment key is generated to uniquely
        # identify this particular time the server is running.
        self.deployment_key = zato_deployment_key

        # This is to handle SIGURG signals.
        if is_posix:
            register_diag_handlers()

        # Configure paths and load data pertaining to Zato KVDB
        self.set_up_zato_kvdb()

        # Find out if we are on a platform that can handle our posix_ipc
        _skip_platform = self.fs_server_config.misc.get('posix_ipc_skip_platform')
        _skip_platform = _skip_platform if isinstance(_skip_platform, list) else [_skip_platform]
        _skip_platform = [elem for elem in _skip_platform if elem]
        self.fs_server_config.misc.posix_ipc_skip_platform = _skip_platform

        # Create all POSIX IPC objects now that we have the deployment key,
        # but only if our platform allows it.
        if self.has_posix_ipc:
            self.shmem_size = int(float(self.fs_server_config.shmem.size) * 10**6) # Convert to megabytes as integer
            self.server_startup_ipc.create(self.deployment_key, self.shmem_size)
            self.connector_config_ipc.create(self.deployment_key, self.shmem_size)
        else:
            self.server_startup_ipc = None
            self.connector_config_ipc = None

        # Store the ODB configuration, create an ODB connection pool and have self.odb use it
        self.config.odb_data = self.get_config_odb_data(self)
        self.set_up_odb()

        # Now try grabbing the basic server's data from the ODB. No point
        # in doing anything else if we can't get past this point.
        server = self.odb.fetch_server(self.config.odb_data)

        if not server:
            raise Exception('Server does not exist in the ODB')

        # Set up the server-wide default lock manager
        odb_data = self.config.odb_data

        if is_posix:
            backend_type = 'fcntl' if odb_data.engine == 'sqlite' else odb_data.engine
        else:
            backend_type = 'zato-pass-through'

        self.zato_lock_manager = LockManager(backend_type, 'zato', self.odb.session)

        # Just to make sure distributed locking is configured correctly
        with self.zato_lock_manager(uuid4().hex):
            pass

        # Basic metadata
        self.id = server.id
        self.name = server.name
        self.cluster = self.odb.cluster
        self.cluster_id = self.cluster.id
        self.cluster_name = self.cluster.name
        self.worker_id = '{}.{}.{}.{}'.format(self.cluster_id, self.id, self.worker_pid, new_cid())

        # SQL post-processing
        ODBPostProcess(self.odb.session(), None, self.cluster_id).run()

        # Set up SQL-based key/value API
        self.kv_data_api = KVDataAPI(self.cluster_id, self.odb)

        # Looked up upfront here and assigned to services in their store
        self.enforce_service_invokes = asbool(self.fs_server_config.misc.enforce_service_invokes)

        # For server-to-server RPC
        self.rpc = self.build_server_rpc()

        logger.info(
            'Preferred address of `%s@%s` (pid: %s) is `http%s://%s:%s`',
            self.name, self.cluster_name, self.pid, 's' if use_tls else '', self.preferred_address, self.port)

        # Configure which HTTP methods can be invoked via REST or SOAP channels
        methods_allowed = self.fs_server_config.http.methods_allowed
        methods_allowed = methods_allowed if isinstance(methods_allowed, list) else [methods_allowed]
        self.http_methods_allowed.extend(methods_allowed)

        # As above, as a regular expression to be used in pattern matching
        http_methods_allowed_re = '|'.join(self.http_methods_allowed)
        self.http_methods_allowed_re = '({})'.format(http_methods_allowed_re)

        # Reads in all configuration from ODB
        self.worker_store = WorkerStore(self.config, self)
        self.worker_store.invoke_matcher.read_config(self.fs_server_config.invoke_patterns_allowed)
        self.worker_store.target_matcher.read_config(self.fs_server_config.invoke_target_patterns_allowed)
        self.set_up_config(server)

        # Normalize hot-deploy configuration
        self.hot_deploy_config = Bunch()
        self.hot_deploy_config.pickup_dir = absolutize(self.fs_server_config.hot_deploy.pickup_dir, self.repo_location)
        self.hot_deploy_config.work_dir = self.work_dir
        self.hot_deploy_config.backup_history = int(self.fs_server_config.hot_deploy.backup_history)
        self.hot_deploy_config.backup_format = self.fs_server_config.hot_deploy.backup_format

        # The first name was used prior to v3.2, note pick_up vs. pickup
        if 'delete_after_pick_up':
            delete_after_pickup = self.fs_server_config.hot_deploy.get('delete_after_pick_up')
        else:
            delete_after_pickup = self.fs_server_config.hot_deploy.get('delete_after_pickup')

        self.hot_deploy_config.delete_after_pickup = delete_after_pickup

        # Added in 3.1, hence optional
        max_batch_size = int(self.fs_server_config.hot_deploy.get('max_batch_size', 1000))

        # Turn it into megabytes
        max_batch_size = max_batch_size * 1000

        # Finally, assign it to ServiceStore
        self.service_store.max_batch_size = max_batch_size

        # Rate limiting
        self.rate_limiting = RateLimiting()
        self.rate_limiting.cluster_id = self.cluster_id
        self.rate_limiting.global_lock_func = self.zato_lock_manager
        self.rate_limiting.sql_session_func = self.odb.session

        # Set up rate limiting for ConfigDict-based objects, which includes everything except for:
        # * services  - configured in ServiceStore
        # * SSO       - configured in the next call
        self.set_up_rate_limiting()

        # Rate limiting for SSO
        self.set_up_sso_rate_limiting()

        # Some parts of the worker store's configuration are required during the deployment of services
        # which is why we are doing it here, before worker_store.init() is called.
        self.worker_store.early_init()

        # Deploys services
        locally_deployed = self._after_init_common(server)

        # Initializes worker store, including connectors
        self.worker_store.init()
        self.request_dispatcher_dispatch = self.worker_store.request_dispatcher.dispatch

        # Configure remaining parts of SSO
        self.configure_sso()

        # Cannot be done in __init__ because self.sso_config is not available there yet
        salt_size = self.sso_config.hash_secret.salt_size
        self.crypto_manager.add_hash_scheme('zato.default', self.sso_config.hash_secret.rounds, salt_size)

        for name in('current_work_dir', 'backup_work_dir', 'last_backup_work_dir', 'delete_after_pickup'):

            # New in 2.0
            if name == 'delete_after_pickup':

                # For backward compatibility, we need to support both names
                old_name = 'delete_after_pick_up'

                if old_name in self.fs_server_config.hot_deploy:
                    _name = old_name
                else:
                    _name = name

                value = asbool(self.fs_server_config.hot_deploy.get(_name, True))
                self.hot_deploy_config[name] = value
            else:
                self.hot_deploy_config[name] = os.path.normpath(os.path.join(
                    self.hot_deploy_config.work_dir, self.fs_server_config.hot_deploy[name]))

        self.broker_client = BrokerClient(self.rpc, self.fs_server_config.scheduler)
        self.worker_store.set_broker_client(self.broker_client)

        self._after_init_accepted(locally_deployed)
        self.odb.server_up_down(
            server.token, SERVER_UP_STATUS.RUNNING, True, self.host, self.port, self.preferred_address, use_tls)

        # These flags are needed if we are the first worker or not
        has_ibm_mq = bool(self.worker_store.worker_config.definition_wmq.keys()) \
            and self.fs_server_config.component_enabled.ibm_mq

        has_sftp = bool(self.worker_store.worker_config.out_sftp.keys())

        subprocess_start_config = SubprocessStartConfig()
        subprocess_start_config.has_ibm_mq = has_ibm_mq
        subprocess_start_config.has_sftp = has_sftp

        # Directories for SSH keys used by SFTP channels
        self.sftp_channel_dir = os.path.join(self.repo_location, 'sftp', 'channel')

        # This is the first process
        if self.is_starting_first:

            logger.info('First worker of `%s` is %s', self.name, self.pid)

            self.startup_callable_tool.invoke(SERVER_STARTUP.PHASE.IN_PROCESS_FIRST, kwargs={
                'server': self,
            })

            # Clean up any old WSX connections possibly registered for this server
            # which may be still lingering around, for instance, if the server was previously
            # shut down forcibly and did not have an opportunity to run self.cleanup_on_stop
            self.cleanup_wsx()

            # Startup services
            self.invoke_startup_services()

            # Subprocess-based connectors
            if self.has_posix_ipc:
                self.init_subprocess_connectors(subprocess_start_config)

            # SFTP channels are new in 3.1 and the directories may not exist
            if not os.path.exists(self.sftp_channel_dir):
                os.makedirs(self.sftp_channel_dir)

        # These are subsequent processes
        else:
            self.startup_callable_tool.invoke(SERVER_STARTUP.PHASE.IN_PROCESS_OTHER, kwargs={
                'server': self,
            })

            if self.has_posix_ipc:
                self._populate_connector_config(subprocess_start_config)

        # IPC
        self.ipc_api.name = self.ipc_api.get_endpoint_name(self.cluster_name, self.name, self.pid)
        self.ipc_api.pid = self.pid
        self.ipc_api.on_message_callback = self.worker_store.on_ipc_message

        if is_posix:
            spawn_greenlet(self.ipc_api.run)

            events_config = self.connector_config_ipc.get_config(ZatoEventsIPC.ipc_config_name, as_dict=True) # type: dict
            events_tcp_port = events_config['port']

            # Statistics
            self._run_stats_client(events_tcp_port)

        # Invoke startup callables
        self.startup_callable_tool.invoke(SERVER_STARTUP.PHASE.AFTER_STARTED, kwargs={
            'server': self,
        })

        logger.info('Started `%s@%s` (pid: %s)', server.name, server.cluster.name, self.pid)

# ################################################################################################################################

    def _populate_connector_config(self, config):
        """ Called when we are not the first worker and, if any connector is enabled,
        we need to get its configuration through IPC and populate our own accordingly.
        """
        # type: (SubprocessStartConfig)

        ipc_config_name_to_enabled = {
            IBMMQIPC.ipc_config_name: config.has_ibm_mq,
            SFTPIPC.ipc_config_name: config.has_sftp,
            ZatoEventsIPC.ipc_config_name: True,
        }

        for ipc_config_name, is_enabled in ipc_config_name_to_enabled.items():
            if is_enabled:
                response = self.connector_config_ipc.get_config(ipc_config_name)
                if response:
                    response = loads(response)
                    connector_suffix = ipc_config_name.replace('zato-', '').replace('-', '_')
                    connector_attr = 'connector_{}'.format(connector_suffix)
                    connector = getattr(self, connector_attr) # type: SubprocessIPC
                    connector.ipc_tcp_port = response['port']

# ################################################################################################################################

    def init_subprocess_connectors(self, config):
        """ Sets up subprocess-based connectors.
        """
        # type: (SubprocessStartConfig)

        # Common
        ipc_tcp_start_port = int(self.fs_server_config.misc.get('ipc_tcp_start_port', 34567))

        # IBM MQ
        if config.has_ibm_mq:

            # Will block for a few seconds at most, until is_ok is returned
            # which indicates that a connector started or not.
            try:
                if self.connector_ibm_mq.start_ibm_mq_connector(ipc_tcp_start_port):
                    self.connector_ibm_mq.create_initial_wmq_definitions(self.worker_store.worker_config.definition_wmq)
                    self.connector_ibm_mq.create_initial_wmq_outconns(self.worker_store.worker_config.out_wmq)
                    self.connector_ibm_mq.create_initial_wmq_channels(self.worker_store.worker_config.channel_wmq)
            except Exception as e:
                logger.warning('Could not create initial IBM MQ objects, e:`%s`', e)
            else:
                self.subproc_current_state.is_ibm_mq_running = True

        # SFTP
        if config.has_sftp and self.connector_sftp.start_sftp_connector(ipc_tcp_start_port):
            self.connector_sftp.create_initial_sftp_outconns(self.worker_store.worker_config.out_sftp)
            self.subproc_current_state.is_sftp_running = True

        # Prepare Zato events configuration
        events_config = self.fs_server_config.get('events') or {} # type: dict

        # This is optional in server.conf ..
        fs_data_path = events_config.get('fs_data_path') or ''
        fs_data_path = fs_data_path or EventsDefault.fs_data_path

        # An absolute path = someone chose it explicitly, we leave it is as it is.
        if os.path.isabs(fs_data_path):
            pass

        # .. otherwise, build a full path.
        else:
            fs_data_path = os.path.join(self.work_dir, fs_data_path, self.events_dir, 'zato.events')
            fs_data_path = os.path.abspath(fs_data_path)
            fs_data_path = os.path.normpath(fs_data_path)

        extra_options_kwargs = {
            'fs_data_path': fs_data_path,
            'sync_threshold': EventsDefault.sync_threshold,
            'sync_interval': EventsDefault.sync_interval,
        }

        # Zato events connector always starts
        self.connector_events.start_zato_events_connector(ipc_tcp_start_port, extra_options_kwargs=extra_options_kwargs)

        # Wait until the events connector started - this will let other parts
        # of the server assume that it is always available.
        wait_until_port_taken(self.connector_events.ipc_tcp_port, timeout=5)

# ################################################################################################################################

    def set_up_sso_rate_limiting(self):
        for item in self.odb.get_sso_user_rate_limiting_info():
            self._create_sso_user_rate_limiting(item.user_id, True, item.rate_limit_def)

# ################################################################################################################################

    def _create_sso_user_rate_limiting(self, user_id, is_active, rate_limit_def, _type=RATE_LIMIT.OBJECT_TYPE.SSO_USER):
        self.rate_limiting.create({
            'id': user_id,
            'type_': _type,
            'name': user_id,
            'is_active': is_active,
            'parent_type': None,
            'parent_name': None,
        }, rate_limit_def, True)

# ################################################################################################################################

    def _get_sso_session(self):
        """ Returns a session function suitable for SSO operations.
        """
        pool_name = self.sso_config.sql.name
        if pool_name:
            try:
                pool = self.worker_store.sql_pool_store.get(pool_name)
            except KeyError:
                pool = None
            if not pool:
                raise Exception('SSO pool `{}` not found or inactive'.format(pool_name))
            else:
                session_func = pool.session
        else:
            session_func = self.odb.session

        return session_func()

# ################################################################################################################################

    def configure_sso(self):
        if self.is_sso_enabled:
            self.sso_api.post_configure(self._get_sso_session, self.odb.is_sqlite)

# ################################################################################################################################

    def invoke_startup_services(self):
        stanza = 'startup_services_first_worker' if self.is_starting_first else 'startup_services_any_worker'
        _invoke_startup_services('Parallel', stanza,
            self.fs_server_config, self.repo_location, self.broker_client, None,
            is_sso_enabled=self.is_sso_enabled)

# ################################################################################################################################

    def get_cache(self, cache_type, cache_name):
        """ Returns a cache object of given type and name.
        """
        return self.worker_store.cache_api.get_cache(cache_type, cache_name)

# ################################################################################################################################

    def get_from_cache(self, cache_type, cache_name, key):
        """ Returns a value from input cache by key, or None if there is no such key.
        """
        return self.worker_store.cache_api.get_cache(cache_type, cache_name).get(key)

# ################################################################################################################################

    def set_in_cache(self, cache_type, cache_name, key, value):
        """ Sets a value in cache for input parameters.
        """
        return self.worker_store.cache_api.get_cache(cache_type, cache_name).set(key, value)

# ################################################################################################################################

    def invoke_all_pids(self, service, request, timeout=5, *args, **kwargs):
        """ Invokes a given service in each of processes current server has.
        """
        try:
            # PID -> response from that process
            out = {}

            # Get all current PIDs
            data = self.invoke('zato.info.get-worker-pids', serialize=False).getvalue(False)
            pids = data['response']['pids']

            # Underlying IPC needs strings on input instead of None
            request = request or ''

            for pid in pids:
                response = {
                    'is_ok': False,
                    'pid_data': None,
                    'error_info': None
                }

                try:
                    is_ok, pid_data = self.invoke_by_pid(service, request, pid, timeout=timeout, *args, **kwargs)
                    response['is_ok'] = is_ok
                    response['pid_data' if is_ok else 'error_info'] = pid_data

                except Exception:
                    e = format_exc()
                    response['error_info'] = e
                finally:
                    out[pid] = response
        except Exception:
            logger.warning('PID invocation error `%s`', format_exc())
        finally:
            return out

# ################################################################################################################################

    def invoke_by_pid(self, service, request, target_pid, *args, **kwargs):
        """ Invokes a service in a worker process by the latter's PID.
        """
        return self.ipc_api.invoke_by_pid(service, request, self.cluster_name, self.name, target_pid,
            self.fifo_response_buffer_size, *args, **kwargs)

# ################################################################################################################################

    def invoke(self, service:'str', request:'any_'=None, *args:'any_', **kwargs:'any_') -> 'any_':
        """ Invokes a service either in our own worker or, if PID is given on input, in another process of this server.
        """
        target_pid = kwargs.pop('pid', None)
        if target_pid and target_pid != self.pid:

            # This cannot be used by self.invoke_by_pid
            data_format = kwargs.pop('data_format', None)

            _, data = self.invoke_by_pid(service, request, target_pid, *args, **kwargs)
            return dumps(data) if data_format == DATA_FORMAT.JSON else data
        else:
            return self.worker_store.invoke(
                service, request,
                data_format=kwargs.pop('data_format', DATA_FORMAT.DICT),
                serialize=kwargs.pop('serialize', True),
                *args, **kwargs)

# ################################################################################################################################

    def publish(self, *args:'any_', **kwargs:'any_') -> 'any_':
        return self.worker_store.pubsub.publish(*args, **kwargs)

# ################################################################################################################################

    def invoke_async(self, service, request, callback, *args, **kwargs):
        """ Invokes a service in background.
        """
        return self.worker_store.invoke(service, request, is_async=True, callback=callback, *args, **kwargs)

# ################################################################################################################################

    def publish_pickup(self, topic_name, request, *args, **kwargs):
        """ Publishes a pickedup file to a named topic.
        """
        self.invoke('zato.pubsub.publish.publish', {
            'topic_name': topic_name,
            'endpoint_id': self.default_internal_pubsub_endpoint_id,
            'has_gd': False,
            'data': dumps({
                'meta': {
                    'pickup_ts_utc': request['ts_utc'],
                    'stanza': request.get('stanza'),
                    'full_path': request['full_path'],
                    'file_name': request['file_name'],
                },
                'data': {
                    'raw': request['raw_data'],
                }
            })
        })

# ################################################################################################################################

    def deliver_pubsub_msg(self, msg):
        """ A callback method invoked by pub/sub delivery tasks for each messages that is to be delivered.
        """
        subscription = self.worker_store.pubsub.subscriptions_by_sub_key[msg.sub_key]
        topic = self.worker_store.pubsub.topics[subscription.config.topic_id]

        if topic.before_delivery_hook_service_invoker:
            response = topic.before_delivery_hook_service_invoker(topic, msg)
            if response['skip_msg']:
                raise SkipDelivery(msg.pub_msg_id)

        self.invoke('zato.pubsub.delivery.deliver-message', {'msg':msg, 'subscription':subscription})

# ################################################################################################################################

    def encrypt(self, data:'any_', prefix:'str'=SECRETS.PREFIX) -> 'str':
        """ Returns data encrypted using server's CryptoManager.
        """
        if data:
            data = data.encode('utf8') if isinstance(data, unicode) else data
            encrypted = self.crypto_manager.encrypt(data)
            encrypted = encrypted.decode('utf8')
            return '{}{}'.format(prefix, encrypted)

# ################################################################################################################################

    def hash_secret(self, data, name='zato.default'):
        return self.crypto_manager.hash_secret(data, name)

# ################################################################################################################################

    def verify_hash(self, given, expected, name='zato.default'):
        return self.crypto_manager.verify_hash(given, expected, name)

# ################################################################################################################################

    def decrypt(self, data, _prefix=SECRETS.PREFIX, _marker=SECRETS.EncryptedMarker):
        """ Returns data decrypted using server's CryptoManager.
        """

        if isinstance(data, bytes):
            data = data.decode('utf8')

        if data.startswith((_prefix, _marker)):
            return self.decrypt_no_prefix(data.replace(_prefix, '', 1))
        else:
            return data # Already decrypted, return as is

# ################################################################################################################################

    def decrypt_no_prefix(self, data):
        return self.crypto_manager.decrypt(data)

# ################################################################################################################################

    def set_up_zato_kvdb(self):

        self.kvdb_dir = os.path.join(self.work_dir, 'kvdb', 'v10')

        if not os.path.exists(self.kvdb_dir):
            os.makedirs(self.kvdb_dir, exist_ok=True)

        self.load_zato_kvdb_data()

# ################################################################################################################################

    def load_zato_kvdb_data(self):

        #
        # Only now do we know what the full paths for KVDB data are so we can set them accordingly here ..
        #

        self.slow_responses.set_data_path(
            os.path.join(self.kvdb_dir, CommonZatoKVDB.SlowResponsesPath),
        )

        self.usage_samples.set_data_path(
            os.path.join(self.kvdb_dir, CommonZatoKVDB.UsageSamplesPath),
        )

        self.current_usage.set_data_path(
            os.path.join(self.kvdb_dir, CommonZatoKVDB.CurrentUsagePath),
        )

        self.pub_sub_metadata.set_data_path(
            os.path.join(self.kvdb_dir, CommonZatoKVDB.PubSubMetadataPath),
        )

        #
        # .. and now we can load all the data.
        #

        self.slow_responses.load_data()
        self.usage_samples.load_data()
        self.current_usage.load_data()
        self.pub_sub_metadata.load_data()

# ################################################################################################################################

    def save_zato_main_proc_state(self):
        self.slow_responses.save_data()
        self.usage_samples.save_data()
        self.current_usage.save_data()
        self.pub_sub_metadata.save_data()

# ################################################################################################################################

    @staticmethod
    def post_fork(arbiter, worker):
        """ A Gunicorn hook which initializes the worker.
        """

        # Each subprocess needs to have the random number generator re-seeded.
        random_seed()

        worker.app.zato_wsgi_app.startup_callable_tool.invoke(SERVER_STARTUP.PHASE.BEFORE_POST_FORK, kwargs={
            'arbiter': arbiter,
            'worker': worker,
        })

        worker.app.zato_wsgi_app.worker_pid = worker.pid
        ParallelServer.start_server(worker.app.zato_wsgi_app, arbiter.zato_deployment_key)

# ################################################################################################################################

    @staticmethod
    def on_starting(arbiter):
        """ A Gunicorn hook for setting the deployment key for this particular
        set of server processes. It needs to be added to the arbiter because
        we want for each worker to be (re-)started to see the same key.
        """
        arbiter.zato_deployment_key = '{}.{}'.format(datetime.utcnow().isoformat(), uuid4().hex)

# ################################################################################################################################

    @staticmethod
    def worker_exit(arbiter, worker):

        # Invoke cleanup procedures
        app = worker.app.zato_wsgi_app # type: ParallelServer
        app.cleanup_on_stop()

# ################################################################################################################################

    @staticmethod
    def before_pid_kill(arbiter, worker):
        pass

# ################################################################################################################################

    def cleanup_wsx(self, needs_pid=False):
        """ Delete persistent information about WSX clients currently registered with the server.
        """
        wsx_service = 'zato.channel.web-socket.client.delete-by-server'

        if self.service_store.is_deployed(wsx_service):
            self.invoke(wsx_service, {'needs_pid': needs_pid})

# ################################################################################################################################

    def cleanup_on_stop(self):
        """ A shutdown cleanup procedure.
        """

        # Tell the ODB we've gone through a clean shutdown but only if this is
        # the main process going down (Arbiter) not one of Gunicorn workers.
        # We know it's the main process because its ODB's session has never
        # been initialized.
        if not self.odb.session_initialized:

            self.config.odb_data = self.get_config_odb_data(self)
            self.config.odb_data['fs_sql_config'] = self.fs_sql_config
            self.set_up_odb()

            self.odb.init_session(ZATO_ODB_POOL_NAME, self.config.odb_data, self.odb.pool, False)

            self.odb.server_up_down(self.odb.token, SERVER_UP_STATUS.CLEAN_DOWN)
            self.odb.close()

        # Per-worker cleanup
        else:

            # Store Zato KVDB data on disk
            self.save_zato_main_proc_state()

            # Set the flag to True only the first time we are called, otherwise simply return
            if self._is_process_closing:
                return
            else:
                self._is_process_closing = True

            # Close SQL pools
            self.sql_pool_store.cleanup_on_stop()

            # Close all POSIX IPC structures
            if self.has_posix_ipc:
                self.server_startup_ipc.close()
                self.connector_config_ipc.close()

            # Close ZeroMQ-based IPC
            self.ipc_api.close()

            # WSX connections for this server cleanup
            self.cleanup_wsx(True)

            logger.info('Stopping server process (%s:%s) (%s)', self.name, self.pid, os.getpid())

# ################################################################################################################################

    def notify_new_package(self, package_id):
        """ Publishes a message on the broker so all the servers (this one including
        can deploy a new package).
        """
        msg = {'action': HOT_DEPLOY.CREATE_SERVICE.value, 'package_id': package_id}
        self.broker_client.publish(msg)

# ################################################################################################################################
# ################################################################################################################################
