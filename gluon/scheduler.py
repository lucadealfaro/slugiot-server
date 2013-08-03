#!/usr/bin/env python
# -*- coding: utf-8 -*-

USAGE = """
## Example

For any existing app

Create File: app/models/scheduler.py ======
from gluon.scheduler import Scheduler

def demo1(*args,**vars):
    print 'you passed args=%s and vars=%s' % (args, vars)
    return 'done!'

def demo2():
    1/0

scheduler = Scheduler(db,dict(demo1=demo1,demo2=demo2))
## run worker nodes with:

   cd web2py
   python web2py.py -K myapp
or
   python gluon/scheduler.py -u sqlite://storage.sqlite \
                             -f applications/myapp/databases/ \
                             -t mytasks.py
(-h for info)
python scheduler.py -h

## schedule jobs using
http://127.0.0.1:8000/myapp/appadmin/insert/db/scheduler_task

## monitor scheduled jobs
http://127.0.0.1:8000/myapp/appadmin/select/db?query=db.scheduler_task.id>0

## view completed jobs
http://127.0.0.1:8000/myapp/appadmin/select/db?query=db.scheduler_run.id>0

## view workers
http://127.0.0.1:8000/myapp/appadmin/select/db?query=db.scheduler_worker.id>0

## To install the scheduler as a permanent daemon on Linux (w/ Upstart), put
## the following into /etc/init/web2py-scheduler.conf:
## (This assumes your web2py instance is installed in <user>'s home directory,
## running as <user>, with app <myapp>, on network interface eth0.)

description "web2py task scheduler"
start on (local-filesystems and net-device-up IFACE=eth0)
stop on shutdown
respawn limit 8 60 # Give up if restart occurs 8 times in 60 seconds.
exec sudo -u <user> python /home/<user>/web2py/web2py.py -K <myapp>
respawn

## You can then start/stop/restart/check status of the daemon with:
sudo start web2py-scheduler
sudo stop web2py-scheduler
sudo restart web2py-scheduler
sudo status web2py-scheduler
"""

import os
import time
import multiprocessing
import sys
import threading
import traceback
import signal
import socket
import datetime
import logging
import optparse
import types
import Queue

path = os.getcwd()

if 'WEB2PY_PATH' not in os.environ:
    os.environ['WEB2PY_PATH'] = path

try:
    from gluon.contrib.simplejson import loads, dumps
except:
    from simplejson import loads, dumps

IDENTIFIER = "%s#%s" % (socket.gethostname(),os.getpid())

logger = logging.getLogger('web2py.scheduler.%s' % IDENTIFIER)

from gluon import DAL, Field, IS_NOT_EMPTY, IS_IN_SET, IS_NOT_IN_DB
from gluon import IS_INT_IN_RANGE, IS_DATETIME
from gluon.utils import web2py_uuid
from gluon.storage import Storage


QUEUED = 'QUEUED'
ASSIGNED = 'ASSIGNED'
RUNNING = 'RUNNING'
COMPLETED = 'COMPLETED'
FAILED = 'FAILED'
TIMEOUT = 'TIMEOUT'
STOPPED = 'STOPPED'
ACTIVE = 'ACTIVE'
TERMINATE = 'TERMINATE'
DISABLED = 'DISABLED'
KILL = 'KILL'
PICK = 'PICK'
STOP_TASK = 'STOP_TASK'
EXPIRED = 'EXPIRED'
SECONDS = 1
HEARTBEAT = 3 * SECONDS
MAXHIBERNATION = 10
CLEAROUT = '!clear!'

CALLABLETYPES = (types.LambdaType, types.FunctionType,
                 types.BuiltinFunctionType,
                 types.MethodType, types.BuiltinMethodType)


class Task(object):
    def __init__(self, app, function, timeout, args='[]', vars='{}', **kwargs):
        logger.debug(' new task allocated: %s.%s', app, function)
        self.app = app
        self.function = function
        self.timeout = timeout
        self.args = args  # json
        self.vars = vars  # json
        self.__dict__.update(kwargs)

    def __str__(self):
        return '<Task: %s>' % self.function


class TaskReport(object):
    def __init__(self, status, result=None, output=None, tb=None):
        logger.debug('    new task report: %s', status)
        if tb:
            logger.debug('   traceback: %s', tb)
        else:
            logger.debug('   result: %s', result)
        self.status = status
        self.result = result
        self.output = output
        self.tb = tb

    def __str__(self):
        return '<TaskReport: %s>' % self.status


def demo_function(*argv, **kwargs):
    """ test function """
    for i in range(argv[0]):
        print 'click', i
        time.sleep(1)
    return 'done'

#the two functions below deal with simplejson decoding as unicode, esp for the dict decode
#and subsequent usage as function Keyword arguments unicode variable names won't work!
#borrowed from http://stackoverflow.com/questions/956867/how-to-get-string-objects-instead-unicode-ones-from-json-in-python


def _decode_list(lst):
    newlist = []
    for i in lst:
        if isinstance(i, unicode):
            i = i.encode('utf-8')
        elif isinstance(i, list):
            i = _decode_list(i)
        newlist.append(i)
    return newlist


def _decode_dict(dct):
    newdict = {}
    for k, v in dct.iteritems():
        if isinstance(k, unicode):
            k = k.encode('utf-8')
        if isinstance(v, unicode):
            v = v.encode('utf-8')
        elif isinstance(v, list):
            v = _decode_list(v)
        newdict[k] = v
    return newdict


def executor(queue, task, out):
    """ the background process """
    logger.debug('    task started')

    class LogOutput(object):
        """Facility to log output at intervals"""
        def __init__(self, out_queue):
            self.out_queue = out_queue
            self.stdout = sys.stdout
            sys.stdout = self

        def __del__(self):
            sys.stdout = self.stdout

        def flush(self):
            pass

        def write(self, data):
            self.out_queue.put(data)

    W2P_TASK = Storage({'id' : task.task_id, 'uuid' : task.uuid})
    stdout = LogOutput(out)
    try:
        if task.app:
            os.chdir(os.environ['WEB2PY_PATH'])
            from gluon.shell import env, parse_path_info
            from gluon import current
            level = logging.getLogger().getEffectiveLevel()
            logging.getLogger().setLevel(logging.WARN)
            # Get controller-specific subdirectory if task.app is of
            # form 'app/controller'
            (a, c, f) = parse_path_info(task.app)
            _env = env(a=a, c=c, import_models=True)
            logging.getLogger().setLevel(level)
            f = task.function
            functions = current._scheduler.tasks
            if not functions:
                #look into env
                _function = _env.get(f)
            else:
                _function = functions.get(f)
            if not isinstance(_function, CALLABLETYPES):
                raise NameError(
                    "name '%s' not found in scheduler's environment" % f)
            #Inject W2P_TASK into environment
            _env.update({'W2P_TASK' : W2P_TASK})
            globals().update(_env)
            args = loads(task.args)
            vars = loads(task.vars, object_hook=_decode_dict)
            result = dumps(_function(*args, **vars))
        else:
            ### for testing purpose only
            result = eval(task.function)(
                *loads(task.args, object_hook=_decode_dict),
                **loads(task.vars, object_hook=_decode_dict))
        queue.put(TaskReport(COMPLETED, result=result))
    except BaseException, e:
        tb = traceback.format_exc()
        queue.put(TaskReport(FAILED, tb=tb))
    del stdout


class MetaScheduler(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.process = None     # the background process
        self.have_heartbeat = True   # set to False to kill
        self.empty_runs = 0


    def async(self, task):
        """
        starts the background process and returns:
        ('ok',result,output)
        ('error',exception,None)
        ('timeout',None,None)
        ('terminated',None,None)
        """
        db = self.db
        sr = db.scheduler_run
        out = multiprocessing.Queue()
        queue = multiprocessing.Queue(maxsize=1)
        p = multiprocessing.Process(target=executor, args=(queue, task, out))
        self.process = p
        logger.debug('   task starting')
        p.start()

        task_output = ""
        tout = ""

        try:
            if task.sync_output > 0:
                run_timeout = task.sync_output
            else:
                run_timeout = task.timeout

            start = time.time()

            while p.is_alive() and (
                    not task.timeout or time.time() - start < task.timeout):
                if tout:
                    try:
                        logger.debug(' partial output saved')
                        db(sr.id == task.run_id).update(run_output=task_output)
                        db.commit()
                    except:
                        pass
                p.join(timeout=run_timeout)
                tout = ""
                while not out.empty():
                    tout += out.get()
                if tout:
                    logger.debug(' partial output: "%s"' % str(tout))
                    if CLEAROUT in tout:
                        task_output = tout[
                            tout.rfind(CLEAROUT) + len(CLEAROUT):]
                    else:
                        task_output += tout
        except:
            p.terminate()
            p.join()
            self.have_heartbeat = False
            logger.debug('    task stopped by general exception')
            tr = TaskReport(STOPPED)
        else:
            if p.is_alive():
                p.terminate()
                logger.debug('    task timeout')
                try:
                    # we try to get a traceback here
                    tr = queue.get(timeout=2)
                    tr.status = TIMEOUT
                    tr.output = task_output
                except Queue.Empty:
                    tr = TaskReport(TIMEOUT)
            elif queue.empty():
                self.have_heartbeat = False
                logger.debug('    task stopped')
                tr = TaskReport(STOPPED)
            else:
                logger.debug('  task completed or failed')
                tr = queue.get()
        tr.output = task_output
        return tr

    def die(self):
        logger.info('die!')
        self.have_heartbeat = False
        self.terminate_process()

    def give_up(self):
        logger.info('Giving up as soon as possible!')
        self.have_heartbeat = False

    def terminate_process(self):
        try:
            self.process.terminate()
        except:
            pass  # no process to terminate

    def run(self):
        """ the thread that sends heartbeat """
        counter = 0
        while self.have_heartbeat:
            self.send_heartbeat(counter)
            counter += 1

    def start_heartbeats(self):
        self.start()

    def send_heartbeat(self, counter):
        print 'thum'
        time.sleep(1)

    def pop_task(self):
        return Task(
            app=None,
            function='demo_function',
            timeout=7,
            args='[2]',
            vars='{}')

    def report_task(self, task, task_report):
        print 'reporting task'
        pass

    def sleep(self):
        pass

    def loop(self):
        try:
            self.start_heartbeats()
            while True and self.have_heartbeat:
                logger.debug('looping...')
                task = self.pop_task()
                if task:
                    self.empty_runs = 0
                    self.report_task(task, self.async(task))
                else:
                    self.empty_runs += 1
                    logger.debug('sleeping...')
                    if self.max_empty_runs != 0:
                        logger.debug('empty runs %s/%s',
                                     self.empty_runs, self.max_empty_runs)
                        if self.empty_runs >= self.max_empty_runs:
                            logger.info(
                                'empty runs limit reached, killing myself')
                            self.die()
                    self.sleep()
        except KeyboardInterrupt:
            self.die()


TASK_STATUS = (QUEUED, RUNNING, COMPLETED, FAILED, TIMEOUT, STOPPED, EXPIRED)
RUN_STATUS = (RUNNING, COMPLETED, FAILED, TIMEOUT, STOPPED)
WORKER_STATUS = (ACTIVE, PICK, DISABLED, TERMINATE, KILL, STOP_TASK)


class TYPE(object):
    """
    validator that check whether field is valid json and validate its type
    """

    def __init__(self, myclass=list, parse=False):
        self.myclass = myclass
        self.parse = parse

    def __call__(self, value):
        from gluon import current
        try:
            obj = loads(value)
        except:
            return (value, current.T('invalid json'))
        else:
            if isinstance(obj, self.myclass):
                if self.parse:
                    return (obj, None)
                else:
                    return (value, None)
            else:
                return (value, current.T('Not of type: %s') % self.myclass)


class Scheduler(MetaScheduler):
    def __init__(self, db, tasks=None, migrate=True,
                 worker_name=None, group_names=['main'], heartbeat=HEARTBEAT,
                 max_empty_runs=0, discard_results=False, utc_time=False):

        MetaScheduler.__init__(self)

        self.db = db
        self.db_thread = None
        self.tasks = tasks
        self.group_names = group_names
        self.heartbeat = heartbeat
        self.worker_name = worker_name or IDENTIFIER
        #list containing status as recorded in the table plus a boost parameter
        #for hibernation (i.e. when someone stop the worker acting on the worker table)
        self.worker_status = [RUNNING, 1]
        self.max_empty_runs = max_empty_runs
        self.discard_results = discard_results
        self.is_a_ticker = False
        self.do_assign_tasks = False
        self.greedy = False
        self.utc_time = utc_time

        from gluon import current
        current._scheduler = self

        self.define_tables(db, migrate=migrate)

    def now(self):
        return self.utc_time and datetime.datetime.utcnow() or datetime.datetime.now()

    def set_requirements(self, scheduler_task):
        from gluon import current
        if hasattr(current, 'request'):
            scheduler_task.application_name.default = '%s/%s' % (
                current.request.application, current.request.controller
            )

    def define_tables(self, db, migrate):
        from gluon.dal import DEFAULT
        logger.debug('defining tables (migrate=%s)', migrate)
        now = self.now
        db.define_table(
            'scheduler_task',
            Field('application_name', requires=IS_NOT_EMPTY(),
                  default=None, writable=False),
            Field('task_name', default=None),
            Field('group_name', default='main'),
            Field('status', requires=IS_IN_SET(TASK_STATUS),
                  default=QUEUED, writable=False),
            Field('function_name',
                  requires=IS_IN_SET(sorted(self.tasks.keys()))
                  if self.tasks else DEFAULT),
            Field('uuid', length=255,
                  requires=IS_NOT_IN_DB(db, 'scheduler_task.uuid'),
                  unique=True, default=web2py_uuid),
            Field('args', 'text', default='[]', requires=TYPE(list)),
            Field('vars', 'text', default='{}', requires=TYPE(dict)),
            Field('enabled', 'boolean', default=True),
            Field('start_time', 'datetime', default=now,
                  requires=IS_DATETIME()),
            Field('next_run_time', 'datetime', default=now),
            Field('stop_time', 'datetime'),
            Field('repeats', 'integer', default=1, comment="0=unlimited",
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('retry_failed', 'integer', default=0, comment="-1=unlimited",
                  requires=IS_INT_IN_RANGE(-1, None)),
            Field('period', 'integer', default=60, comment='seconds',
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('timeout', 'integer', default=60, comment='seconds',
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('sync_output', 'integer', default=0,
                  comment="update output every n sec: 0=never",
                  requires=IS_INT_IN_RANGE(0, None)),
            Field('times_run', 'integer', default=0, writable=False),
            Field('times_failed', 'integer', default=0, writable=False),
            Field('last_run_time', 'datetime', writable=False, readable=False),
            Field('assigned_worker_name', default='', writable=False),
            on_define=self.set_requirements,
            migrate=migrate, format='%(task_name)s')

        db.define_table(
            'scheduler_run',
            Field('task_id', 'reference scheduler_task'),
            Field('status', requires=IS_IN_SET(RUN_STATUS)),
            Field('start_time', 'datetime'),
            Field('stop_time', 'datetime'),
            Field('run_output', 'text'),
            Field('run_result', 'text'),
            Field('traceback', 'text'),
            Field('worker_name', default=self.worker_name),
            migrate=migrate)

        db.define_table(
            'scheduler_worker',
            Field('worker_name', length=255, unique=True),
            Field('first_heartbeat', 'datetime'),
            Field('last_heartbeat', 'datetime'),
            Field('status', requires=IS_IN_SET(WORKER_STATUS)),
            Field('is_ticker', 'boolean', default=False, writable=False),
            Field('group_names', 'list:string', default=self.group_names),#FIXME writable=False or give the chance to update dinamically the groups?
            migrate=migrate)
        if migrate:
            db.commit()

    def loop(self, worker_name=None):
        signal.signal(signal.SIGTERM, lambda signum, stack_frame: sys.exit(1))
        try:
            self.start_heartbeats()
            while True and self.have_heartbeat:
                if self.worker_status[0] == DISABLED:
                    logger.debug('Someone stopped me, sleeping until better times come (%s)', self.worker_status[1])
                    self.sleep()
                    continue
                logger.debug('looping...')
                task = self.wrapped_pop_task()
                if task:
                    self.empty_runs = 0
                    self.worker_status[0] = RUNNING
                    self.report_task(task, self.async(task))
                    self.worker_status[0] = ACTIVE
                else:
                    self.empty_runs += 1
                    logger.debug('sleeping...')
                    if self.max_empty_runs != 0:
                        logger.debug('empty runs %s/%s',
                                     self.empty_runs, self.max_empty_runs)
                        if self.empty_runs >= self.max_empty_runs:
                            logger.info(
                                'empty runs limit reached, killing myself')
                            self.die()
                    self.sleep()
        except (KeyboardInterrupt, SystemExit):
            logger.info('catched')
            self.die()

    def wrapped_assign_tasks(self, db):
        logger.debug('Assigning tasks...')
        db.commit()  #db.commit() only for Mysql
        x = 0
        while x < 10:
            try:
                self.assign_tasks(db)
                db.commit()
                logger.debug('Tasks assigned...')
                break
            except:
                db.rollback()
                logger.error('TICKER: error assigning tasks (%s)', x)
                x += 1
                time.sleep(0.5)

    def wrapped_pop_task(self):
        db = self.db
        db.commit() #another nifty db.commit() only for Mysql
        x = 0
        while x < 10:
            try:
                rtn = self.pop_task(db)
                return rtn
                break
            except:
                db.rollback()
                logger.error('    error popping tasks')
                x += 1
                time.sleep(0.5)

    def pop_task(self, db):
        now = self.now()
        st = self.db.scheduler_task
        if self.is_a_ticker and self.do_assign_tasks:
            #I'm a ticker, and 5 loops passed without reassigning tasks, let's do
            #that and loop again
            self.wrapped_assign_tasks(db)
            return None
        #ready to process something
        grabbed = db(st.assigned_worker_name == self.worker_name)(
            st.status == ASSIGNED)

        task = grabbed.select(limitby=(0, 1), orderby=st.next_run_time).first()
        if task:
            task.update_record(status=RUNNING, last_run_time=now)
            #noone will touch my task!
            db.commit()
            logger.debug('   work to do %s', task.id)
        else:
            if self.greedy and self.is_a_ticker:
                #there are other tasks ready to be assigned
                logger.info('TICKER: greedy loop')
                self.wrapped_assign_tasks(db)
            else:
                logger.info('nothing to do')
            return None
        next_run_time = task.last_run_time + datetime.timedelta(
            seconds=task.period)
        times_run = task.times_run + 1
        if times_run < task.repeats or task.repeats == 0:
            #need to run (repeating task)
            run_again = True
        else:
            #no need to run again
            run_again = False
        run_id = 0
        while True and not self.discard_results:
            logger.debug('    new scheduler_run record')
            try:
                run_id = db.scheduler_run.insert(
                    task_id=task.id,
                    status=RUNNING,
                    start_time=now,
                    worker_name=self.worker_name)
                db.commit()
                break
            except:
                time.sleep(0.5)
                db.rollback()
        logger.info('new task %(id)s "%(task_name)s" %(application_name)s.%(function_name)s' % task)
        return Task(
            app=task.application_name,
            function=task.function_name,
            timeout=task.timeout,
            args=task.args,  # in json
            vars=task.vars,  # in json
            task_id=task.id,
            run_id=run_id,
            run_again=run_again,
            next_run_time=next_run_time,
            times_run=times_run,
            stop_time=task.stop_time,
            retry_failed=task.retry_failed,
            times_failed=task.times_failed,
            sync_output=task.sync_output,
            uuid=task.uuid)

    def report_task(self, task, task_report):
        db = self.db
        now = self.now()
        while True:
            try:
                if not self.discard_results:
                    if task_report.result != 'null' or task_report.tb:
                        #result is 'null' as a string if task completed
                        #if it's stopped it's None as NoneType, so we record
                        #the STOPPED "run" anyway
                        logger.debug(' recording task report in db (%s)',
                                     task_report.status)
                        db(db.scheduler_run.id == task.run_id).update(
                            status=task_report.status,
                            stop_time=now,
                            run_result=task_report.result,
                            run_output=task_report.output,
                            traceback=task_report.tb)
                    else:
                        logger.debug(' deleting task report in db because of no result')
                        db(db.scheduler_run.id == task.run_id).delete()
                #if there is a stop_time and the following run would exceed it
                is_expired = (task.stop_time
                              and task.next_run_time > task.stop_time
                              and True or False)
                status = (task.run_again and is_expired and EXPIRED
                          or task.run_again and not is_expired
                          and QUEUED or COMPLETED)
                if task_report.status == COMPLETED:
                    d = dict(status=status,
                             next_run_time=task.next_run_time,
                             times_run=task.times_run,
                             times_failed=0
                             )
                    db(db.scheduler_task.id == task.task_id)(
                        db.scheduler_task.status == RUNNING).update(**d)
                else:
                    st_mapping = {'FAILED': 'FAILED',
                                  'TIMEOUT': 'TIMEOUT',
                                  'STOPPED': 'QUEUED'}[task_report.status]
                    status = (task.retry_failed
                              and task.times_failed < task.retry_failed
                              and QUEUED or task.retry_failed == -1
                              and QUEUED or st_mapping)
                    db(
                        (db.scheduler_task.id == task.task_id) &
                        (db.scheduler_task.status == RUNNING)
                    ).update(
                        times_failed=db.scheduler_task.times_failed + 1,
                        next_run_time=task.next_run_time,
                        status=status
                    )
                db.commit()
                logger.info('task completed (%s)', task_report.status)
                break
            except:
                db.rollback()
                time.sleep(0.5)

    def adj_hibernation(self):
        if self.worker_status[0] == DISABLED:
            wk_st = self.worker_status[1]
            hibernation = wk_st + 1 if wk_st < MAXHIBERNATION else MAXHIBERNATION
            self.worker_status[1] = hibernation

    def send_heartbeat(self, counter):
        if not self.db_thread:
            logger.debug('thread building own DAL object')
            self.db_thread = DAL(
                self.db._uri, folder=self.db._adapter.folder)
            self.define_tables(self.db_thread, migrate=False)
        try:
            db = self.db_thread
            sw, st = db.scheduler_worker, db.scheduler_task
            now = self.now()
            # record heartbeat
            mybackedstatus = db(sw.worker_name == self.worker_name).select().first()
            if not mybackedstatus:
                sw.insert(status=ACTIVE, worker_name=self.worker_name,
                          first_heartbeat=now, last_heartbeat=now,
                          group_names=self.group_names)
                self.worker_status = [ACTIVE, 1]  # activating the process
                mybackedstatus = ACTIVE
            else:
                mybackedstatus = mybackedstatus.status
                if mybackedstatus == DISABLED:
                    # keep sleeping
                    self.worker_status[0] = DISABLED
                    if self.worker_status[1] == MAXHIBERNATION:
                        logger.debug('........recording heartbeat (%s)', self.worker_status[0])
                        db(sw.worker_name == self.worker_name).update(
                            last_heartbeat=now)
                elif mybackedstatus == TERMINATE:
                    self.worker_status[0] = TERMINATE
                    logger.debug("Waiting to terminate the current task")
                    self.give_up()
                    return
                elif mybackedstatus == KILL:
                    self.worker_status[0] = KILL
                    self.die()
                else:
                    if mybackedstatus == STOP_TASK:
                        logger.info('Asked to kill the current task')
                        self.terminate_process()
                    logger.debug('........recording heartbeat (%s)', self.worker_status[0])
                    db(sw.worker_name == self.worker_name).update(
                        last_heartbeat=now, status=ACTIVE)
                    self.worker_status[1] = 1  # re-activating the process
                    if self.worker_status[0] != RUNNING:
                        self.worker_status[0] = ACTIVE

            self.do_assign_tasks = False
            if counter % 5 == 0 or mybackedstatus == PICK:
                try:
                    # delete inactive workers
                    expiration = now - datetime.timedelta(seconds=self.heartbeat * 3)
                    departure = now - datetime.timedelta(
                        seconds=self.heartbeat * 3 * MAXHIBERNATION)
                    logger.debug(
                        '    freeing workers that have not sent heartbeat')
                    inactive_workers = db(
                        ((sw.last_heartbeat < expiration) & (sw.status == ACTIVE)) |
                        ((sw.last_heartbeat < departure) & (sw.status != ACTIVE))
                    )
                    db(st.assigned_worker_name.belongs(
                        inactive_workers._select(sw.worker_name)))(st.status == RUNNING)\
                        .update(assigned_worker_name='', status=QUEUED)
                    inactive_workers.delete()
                    try:
                        self.is_a_ticker = self.being_a_ticker()
                    except:
                        logger.error('Error coordinating TICKER')
                    if self.worker_status[0] == ACTIVE:
                        self.do_assign_tasks = True
                except:
                    logger.error('Error cleaning up')
            db.commit()
        except:
            logger.error('Error retrieving status')
            db.rollback()
        self.adj_hibernation()
        self.sleep()

    def being_a_ticker(self):
        db = self.db_thread
        sw = db.scheduler_worker
        all_active = db(
            (sw.worker_name != self.worker_name) & (sw.status == ACTIVE)
        ).select()
        ticker = all_active.find(lambda row: row.is_ticker is True).first()
        not_busy = self.worker_status[0] == ACTIVE
        if not ticker:
            #if no other tickers are around
            if not_busy:
                #only if I'm not busy
                db(sw.worker_name == self.worker_name).update(is_ticker=True)
                db(sw.worker_name != self.worker_name).update(is_ticker=False)
                logger.info("TICKER: I'm a ticker")
            else:
                #I'm busy
                if len(all_active) >= 1:
                    #so I'll "downgrade" myself to a "poor worker"
                    db(sw.worker_name == self.worker_name).update(is_ticker=False)
                else:
                    not_busy = True
            db.commit()
            return not_busy
        else:
            logger.info(
                "%s is a ticker, I'm a poor worker" % ticker.worker_name)
            return False

    def assign_tasks(self, db):
        sw, st = db.scheduler_worker, db.scheduler_task
        now = self.now()
        all_workers = db(sw.status == ACTIVE).select()
        #build workers as dict of groups
        wkgroups = {}
        for w in all_workers:
            group_names = w.group_names
            for gname in group_names:
                if gname not in wkgroups:
                    wkgroups[gname] = dict(
                        workers=[{'name': w.worker_name, 'c': 0}])
                else:
                    wkgroups[gname]['workers'].append(
                        {'name': w.worker_name, 'c': 0})
        #set queued tasks that expired between "runs" (i.e., you turned off
        #the scheduler): then it wasn't expired, but now it is
        db(st.status.belongs(
            (QUEUED, ASSIGNED)))(st.stop_time < now).update(status=EXPIRED)

        all_available = db(
            (st.status.belongs((QUEUED, ASSIGNED))) &
            ((st.times_run < st.repeats) | (st.repeats == 0)) &
            (st.start_time <= now) &
            ((st.stop_time == None) | (st.stop_time > now)) &
            (st.next_run_time <= now) &
            (st.enabled == True)
        )
        limit = len(all_workers) * (50 / (len(wkgroups) or 1))
        #if there are a moltitude of tasks, let's figure out a maximum of tasks per worker.
        #this can be adjusted with some added intelligence (like esteeming how many tasks will
        #a worker complete before the ticker reassign them around, but the gain is quite small
        #50 is quite a sweet spot also for fast tasks, with sane heartbeat values
        #NB: ticker reassign tasks every 5 cycles, so if a worker completes his 50 tasks in less
        #than heartbeat*5 seconds, it won't pick new tasks until heartbeat*5 seconds pass.

        #If a worker is currently elaborating a long task, all other tasks assigned
        #to him needs to be reassigned "freely" to other workers, that may be free.
        #this shuffles up things a bit, in order to maintain the idea of a semi-linear scalability

        #let's freeze it up
        db.commit()
        x = 0
        for group in wkgroups.keys():
            tasks = all_available(st.group_name == group).select(
                limitby=(0, limit), orderby = st.next_run_time)
            #let's break up the queue evenly among workers
            for task in tasks:
                x += 1
                gname = task.group_name
                ws = wkgroups.get(gname)
                if ws:
                    counter = 0
                    myw = 0
                    for i, w in enumerate(ws['workers']):
                        if w['c'] < counter:
                            myw = i
                        counter = w['c']
                    d = dict(
                        status=ASSIGNED,
                        assigned_worker_name=wkgroups[gname]['workers'][myw]['name']
                    )
                    if not task.task_name:
                        d['task_name'] = task.function_name
                    task.update_record(**d)
                    wkgroups[gname]['workers'][myw]['c'] += 1

        db.commit()
        #I didn't report tasks but I'm working nonetheless!!!!
        if x > 0:
            self.empty_runs = 0
        #I'll be greedy only if tasks assigned are equal to the limit
        # (meaning there could be others ready to be assigned)
        self.greedy = x >= limit and True or False
        logger.info('TICKER: workers are %s', len(all_workers))
        logger.info('TICKER: tasks are %s', x)

    def sleep(self):
        time.sleep(self.heartbeat * self.worker_status[1])
        # should only sleep until next available task

    def set_worker_status(self, group_names=None, action=ACTIVE):
        if not group_names:
            group_names = self.group_names
        elif isinstance(group_names, str):
            group_names = [group_names]
        for group in group_names:
            self.db(
                self.db.scheduler_worker.group_names.contains(group)
                ).update(status=action)

    def disable(self, group_names=None):
        self.set_worker_status(group_names=group_names,action=DISABLED)

    def resume(self, group_names=None):
        self.set_worker_status(group_names=group_names,action=ACTIVE)

    def terminate(self, group_names=None):
        self.set_worker_status(group_names=group_names,action=TERMINATE)

    def kill(self, group_names=None):
        self.set_worker_status(group_names=group_names,action=KILL)

    def queue_task(self, function, pargs=[], pvars={}, **kwargs):
        """
        Queue tasks. This takes care of handling the validation of all
        values.
        :param function: the function (anything callable with a __name__)
        :param pargs: "raw" args to be passed to the function. Automatically
            jsonified.
        :param pvars: "raw" kwargs to be passed to the function. Automatically
            jsonified
        :param kwargs: all the scheduler_task columns. args and vars here should be
            in json format already, they will override pargs and pvars

        returns a dict just as a normal validate_and_insert, plus a uuid key holding
        the uuid of the queued task. If validation is not passed, both id and uuid
        will be None, and you'll get an "error" dict holding the errors found.
        """
        if hasattr(function, '__name__'):
            function = function.__name__
        targs = 'args' in kwargs and kwargs.pop('args') or dumps(pargs)
        tvars = 'vars' in kwargs and kwargs.pop('vars') or dumps(pvars)
        tuuid = 'uuid' in kwargs and kwargs.pop('uuid') or web2py_uuid()
        tname = 'task_name' in kwargs and kwargs.pop('task_name') or function
        immediate = 'immediate' in kwargs and kwargs.pop('immediate') or None
        rtn = self.db.scheduler_task.validate_and_insert(
            function_name=function,
            task_name=tname,
            args=targs,
            vars=tvars,
            uuid=tuuid,
            **kwargs)
        if not rtn.errors:
            rtn.uuid = tuuid
            if immediate:
                self.db(self.db.scheduler_worker.is_ticker == True).update(status=PICK)
        else:
            rtn.uuid = None
        return rtn

    def task_status(self, ref, output=False):
        """
        Shortcut for task status retrieval

        :param ref: can be
            - integer --> lookup will be done by scheduler_task.id
            - string  --> lookup will be done by scheduler_task.uuid
            - query   --> lookup as you wish (as in db.scheduler_task.task_name == 'test1')
        :param output: fetch also the scheduler_run record

        Returns a single Row object, for the last queued task
        If output == True, returns also the last scheduler_run record
            scheduler_run record is fetched by a left join, so it can
            have all fields == None

        """
        from gluon.dal import Query
        sr, st = self.db.scheduler_run, self.db.scheduler_task
        if isinstance(ref, int):
            q = st.id == ref
        elif isinstance(ref, str):
            q = st.uuid == ref
        elif isinstance(ref, Query):
            q = ref
        else:
            raise SyntaxError(
                "You can retrieve results only by id, uuid or Query")
        fields = [st.ALL]
        left = False
        orderby = ~st.id
        if output:
            fields = st.ALL, sr.ALL
            left = sr.on(sr.task_id == st.id)
            orderby = ~st.id | ~sr.id
        row = self.db(q).select(
            *fields,
            **dict(orderby=orderby,
                   left=left,
                   limitby=(0, 1))
             ).first()
        if row and output:
            row.result = row.scheduler_run.run_result and \
                loads(row.scheduler_run.run_result,
                      object_hook=_decode_dict) or None
        return row

    def stop_task(self, ref):
        """
        Experimental!!!
        Shortcut for task termination.
        If the task is RUNNING it will terminate it --> execution will be set as FAILED
        If the task is QUEUED, its stop_time will be set as to "now",
            the enabled flag will be set to False, status to STOPPED

        :param ref: can be
            - integer --> lookup will be done by scheduler_task.id
            - string  --> lookup will be done by scheduler_task.uuid
        Returns:
            - 1 if task was stopped (meaning an update has been done)
            - None if task was not found, or if task was not RUNNING or QUEUED
        """
        from gluon.dal import Query
        st, sw = self.db.scheduler_task, self.db.scheduler_worker
        if isinstance(ref, int):
            q = st.id == ref
        elif isinstance(ref, str):
            q = st.uuid == ref
        else:
            raise SyntaxError(
                "You can retrieve results only by id or uuid")
        task = self.db(q).select(st.id, st.status, st.assigned_worker_name).first()
        rtn = None
        if not task:
            return rtn
        if task.status == 'RUNNING':
            rtn = self.db(sw.worker_name == task.assigned_worker_name).update(status=STOP_TASK)
        elif task.status == 'QUEUED':
            rtn = self.db(q).update(stop_time=self.now(), enabled=False, status=STOPPED)
        return rtn


def main():
    """
    allows to run worker without python web2py.py .... by simply python this.py
    """
    parser = optparse.OptionParser()
    parser.add_option(
        "-w", "--worker_name", dest="worker_name", default=None,
        help="start a worker with name")
    parser.add_option(
        "-b", "--heartbeat", dest="heartbeat", default=10,
        type='int', help="heartbeat time in seconds (default 10)")
    parser.add_option(
        "-L", "--logger_level", dest="logger_level",
        default=30,
        type='int',
        help="set debug output level (0-100, 0 means all, 100 means none;default is 30)")
    parser.add_option("-E", "--empty-runs",
                      dest="max_empty_runs",
                      type='int',
                      default=0,
                      help="max loops with no grabbed tasks permitted (0 for never check)")
    parser.add_option(
        "-g", "--group_names", dest="group_names",
        default='main',
        help="comma separated list of groups to be picked by the worker")
    parser.add_option(
        "-f", "--db_folder", dest="db_folder",
        default='/Users/mdipierro/web2py/applications/scheduler/databases',
        help="location of the dal database folder")
    parser.add_option(
        "-u", "--db_uri", dest="db_uri",
        default='sqlite://storage.sqlite',
        help="database URI string (web2py DAL syntax)")
    parser.add_option(
        "-t", "--tasks", dest="tasks", default=None,
        help="file containing task files, must define" +
        "tasks = {'task_name':(lambda: 'output')} or similar set of tasks")
    parser.add_option(
        "-U", "--utc-time", dest="utc_time", default=False,
        help="work with UTC timestamps"
    )
    (options, args) = parser.parse_args()
    if not options.tasks or not options.db_uri:
        print USAGE
    if options.tasks:
        path, filename = os.path.split(options.tasks)
        if filename.endswith('.py'):
            filename = filename[:-3]
        sys.path.append(path)
        print 'importing tasks...'
        tasks = __import__(filename, globals(), locals(), [], -1).tasks
        print 'tasks found: ' + ', '.join(tasks.keys())
    else:
        tasks = {}
    group_names = [x.strip() for x in options.group_names.split(',')]

    logging.getLogger().setLevel(options.logger_level)

    print 'groups for this worker: ' + ', '.join(group_names)
    print 'connecting to database in folder: ' + options.db_folder or './'
    print 'using URI: ' + options.db_uri
    db = DAL(options.db_uri, folder=options.db_folder)
    print 'instantiating scheduler...'
    scheduler = Scheduler(db=db,
                          worker_name=options.worker_name,
                          tasks=tasks,
                          migrate=True,
                          group_names=group_names,
                          heartbeat=options.heartbeat,
                          max_empty_runs=options.max_empty_runs,
                          utc_time=options.utc_time)
    signal.signal(signal.SIGTERM, lambda signum, stack_frame: sys.exit(1))
    print 'starting main worker loop...'
    scheduler.loop()

if __name__ == '__main__':
    main()
