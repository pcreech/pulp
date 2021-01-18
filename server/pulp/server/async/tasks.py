import cProfile
from datetime import datetime
import errno
from gettext import gettext as _
import logging
import os
import signal
import time
import traceback
import uuid

from bson.json_util import dumps as bson_dumps
from bson.json_util import loads as bson_loads
from bson import ObjectId
from celery import task, Task as CeleryTask, current_task, __version__ as celery_version
from celery.app import control, defaults
from celery.result import AsyncResult
from mongoengine.queryset import DoesNotExist
from mongoengine.errors import NotUniqueError

from pulp.common.constants import RESOURCE_MANAGER_WORKER_NAME, SCHEDULER_WORKER_NAME
from pulp.common import constants, dateutils, tags
from pulp.plugins.util import misc

from pulp.server.async.celery_instance import celery, RESOURCE_MANAGER_QUEUE, \
    DEDICATED_QUEUE_EXCHANGE
from pulp.server.exceptions import PulpException, MissingResource, \
    NoWorkers, PulpCodedException, error_codes
from pulp.server.config import config
from pulp.server.db.model import Worker, ReservedResource, TaskStatus, \
    ResourceManagerLock, CeleryBeatLock
from pulp.server.managers.repo import _common as common_utils
from pulp.server.managers import factory as managers
from pulp.server.managers.schedule import utils


controller = control.Control(app=celery)
_logger = logging.getLogger(__name__)


class PulpTask(CeleryTask):
    """
    The ancestor of Celery tasks in Pulp. All Celery tasks should inherit from this object.

    It provides behavioral modifications to apply_async and __call__ to serialize and
    deserialize common object types which are not json serializable.
    """

    def _type_transform(self, value):
        """
            Transforms ObjectId types to str type and vice versa.

            Any ObjectId types present are serialized to a str.
            The same str is converted back to an ObjectId while de-serializing.

            :param value: the object to be transformed
            :type  value: Object

            :returns: recursively transformed object
            :rtype: Object
        """
        # Encoding ObjectId to str
        if isinstance(value, ObjectId):
            return bson_dumps(value)

        # Recursive checks inside dict
        if isinstance(value, dict):
            if len(value) == 0:
                return value
            # Decoding '$oid' back to ObjectId
            if '$oid' in value.keys():
                return bson_loads(value)

            return dict((self._type_transform(k), self._type_transform(v))
                        for k, v in value.iteritems())

        # Recursive checks inside a list
        if isinstance(value, list):
            if len(value) == 0:
                return value
            for i, val in enumerate(value):
                value[i] = self._type_transform(val)
            return value

        # Recursive checks inside a tuple
        if isinstance(value, tuple):
            if len(value) == 0:
                return value
            return tuple([self._type_transform(val) for val in value])

        return value

    def apply_async(self, *args, **kwargs):
        """
        Serializes args and kwargs using _type_transform()

        :return: An AsyncResult instance as returned by Celery's apply_async
        :rtype: celery.result.AsyncResult
        """
        args = self._type_transform(args)
        kwargs = self._type_transform(kwargs)
        return super(PulpTask, self).apply_async(*args, **kwargs)

    def __call__(self, *args, **kwargs):
        """
        Deserializes args and kwargs using _type_transform()
        """
        args = self._type_transform(args)
        kwargs = self._type_transform(kwargs)
        return super(PulpTask, self).__call__(*args, **kwargs)


@task(base=PulpTask, acks_late=True)
def _queue_reserved_task_list(name, task_id, resource_id_list, inner_args, inner_kwargs):
    """
    A task that allows multiple resources to be reserved before dispatching a second, "inner", task.
    See _queue_reserved_task for details on the inner workings.

    :param name:                The name of the task to be called
    :type name:                 basestring
    :param inner_task_id:       The UUID to be set on the task being called. By providing
                                the UUID, the caller can have an asynchronous reference to the inner
                                task that will be dispatched.
    :type inner_task_id:        basestring
    :param resource_id_list:    A list of names of the resources you wish to reserve for your task.
                                The system will ensure that no other tasks that want any of the same
                                reservations will run concurrently with yours.
    :type  resource_id_list:    list

    :return: None
    """
    _logger.debug('_queue_reserved_task_list for task %s and ids [%s]' %
                  (task_id, resource_id_list))
    # Find a/the available Worker for processing our list of resources
    worker = get_worker_for_reservation_list(resource_id_list)
    # Reserve each resource, associating them with that Worker
    for rid in resource_id_list:
        _logger.debug('...saving RR for RID %s' % rid)
        ReservedResource(task_id=task_id, worker_name=worker['name'], resource_id=rid).save()

    # Dispatch the Worker
    inner_kwargs['routing_key'] = worker.name
    inner_kwargs['exchange'] = DEDICATED_QUEUE_EXCHANGE
    inner_kwargs['task_id'] = task_id
    try:
        celery.tasks[name].apply_async(*inner_args, **inner_kwargs)
    finally:
        # Arrange to release all held reserved-resources
        _release_resource.apply_async((task_id, ), routing_key=worker.name,
                                      exchange=DEDICATED_QUEUE_EXCHANGE)


@task(base=PulpTask, acks_late=True)
def _queue_reserved_task(name, task_id, resource_id, inner_args, inner_kwargs):
    """
    A task that encapsulates another task to be dispatched later. This task being encapsulated is
    called the "inner" task, and a task name, UUID, and accepts a list of positional args
    and keyword args for the inner task. These arguments are named inner_args and inner_kwargs.
    inner_args is a list, and inner_kwargs is a dictionary passed to the inner task as positional
    and keyword arguments using the * and ** operators.

    The inner task is dispatched into a dedicated queue for a worker that is decided at dispatch
    time. The logic deciding which queue receives a task is controlled through the
    find_worker function.

    :param name:          The name of the task to be called
    :type name:           basestring
    :param inner_task_id: The UUID to be set on the task being called. By providing
                          the UUID, the caller can have an asynchronous reference to the inner task
                          that will be dispatched.
    :type inner_task_id:  basestring
    :param resource_id:   The name of the resource you wish to reserve for your task. The system
                          will ensure that no other tasks that want that same reservation will run
                          concurrently with yours.
    :type  resource_id:   basestring

    :return: None
    """
    while True:
        try:
            worker = get_worker_for_reservation(resource_id)
        except NoWorkers:
            pass
        else:
            break

        try:
            worker = _get_unreserved_worker()
        except NoWorkers:
            pass
        else:
            break

        # No worker is ready for this work, so we need to wait
        time.sleep(0.25)

    ReservedResource(task_id=task_id, worker_name=worker['name'], resource_id=resource_id).save()

    inner_kwargs['routing_key'] = worker.name
    inner_kwargs['exchange'] = DEDICATED_QUEUE_EXCHANGE
    inner_kwargs['task_id'] = task_id

    try:
        celery.tasks[name].apply_async(*inner_args, **inner_kwargs)
    finally:
        _release_resource.apply_async((task_id, ), routing_key=worker.name,
                                      exchange=DEDICATED_QUEUE_EXCHANGE)


def _is_worker(worker_name):
    """
    Strip out workers that should never be assigned work. We need to check
    via "startswith()" since we do not know which host the worker is running on.
    """

    if worker_name.startswith(SCHEDULER_WORKER_NAME) or \
       worker_name.startswith(RESOURCE_MANAGER_QUEUE):
        return False
    return True


def get_worker_for_reservation(resource_id):
    """
    Return the Worker instance that is associated with a reservation of type resource_id. If
    there are no workers with that reservation_id type a pulp.server.exceptions.NoWorkers
    exception is raised.

    :param resource_id:    The name of the resource you wish to reserve for your task.

    :raises NoWorkers:     If all workers have reserved_resource entries associated with them.

    :type resource_id:     basestring
    :returns:              The Worker instance that has a reserved_resource entry of type
                           `resource_id` associated with it.
    :rtype:                pulp.server.db.model.resources.Worker
    """
    reservation = ReservedResource.objects(resource_id=resource_id).first()
    if reservation:
        return Worker.objects(name=reservation['worker_name']).first()
    else:
        raise NoWorkers()


def get_worker_for_reservation_list(resources):
    """
    Return the Worker instance that is associated with the reservations described by the 'resources'
    list. This will be either an existing Worker that is dealing with at least one of the specified
    resources, or an available idle Worker. We sleep and retry the request until it can be
    fulfilled.

    :param resources:   A list of the names of the resources you wish to reserve for your task.

    :type resources:    list
    :returns:           The Worker instance that has a reserved_resource entry associated with it
                        for each resource in 'resources'
    :rtype:             pulp.server.db.model.resources.Worker
    """

    _logger.debug('get_worker_for_reservation_list [%s]' % resources)
    # We leave this loop once we find a Worker to return - otherwise, sleep and try again
    while True:
        reservation_workers = set(
            [reservation['worker_name'] for reservation in
                ReservedResource.objects(resource_id__in=resources)])
        _logger.debug('...num-RR is %d' % len(reservation_workers))
        if len(reservation_workers) == 1:  # Exactly one worker holds any of the desired resources
            _logger.debug('...one-holds')
            return Worker.objects(name=list(reservation_workers)[0]).first()
        elif len(reservation_workers) == 0:  # No worker holds any of the desired resources
            _logger.debug('...zero-holds')
            try:
                worker = _get_unreserved_worker()
                return worker
            except NoWorkers:
                _logger.debug('...unresolved NoWorkers - WAIT')
                pass
        else:
            _logger.debug('...multiple-holds - WAIT')

        time.sleep(0.25)


def _get_unreserved_worker():
    """
    Return the Worker instance that has no reserved_resource entries
    associated with it. If there are no unreserved workers a
    pulp.server.exceptions.NoWorkers exception is raised.

    :raises NoWorkers: If all workers have reserved_resource entries associated with them.

    :returns:          The Worker instance that has no reserved_resource
                       entries associated with it.
    :rtype:            pulp.server.db.model.resources.Worker
    """

    # Build a mapping of queue names to Worker objects
    workers_dict = dict((worker['name'], worker) for worker in Worker.objects.get_online())
    worker_names = workers_dict.keys()
    reserved_names = [r['worker_name'] for r in ReservedResource.objects.all()]

    # Find an unreserved worker using set differences of the names, and filter
    # out workers that should not be assigned work.
    # NB: this is a little messy but set comprehensions are in python 2.7+
    unreserved_workers = set(filter(_is_worker, worker_names)) - set(reserved_names)

    try:
        return workers_dict[unreserved_workers.pop()]
    except KeyError:
        # All workers are reserved
        raise NoWorkers()


def _delete_worker(name, normal_shutdown=False):
    """
    Delete the Worker with _id name from the database, cancel any associated tasks and reservations

    If the worker shutdown normally, no message is logged, otherwise an error level message is
    logged. Default is to assume the worker did not shut down normally.

    Any resource reservations associated with this worker are cleaned up by this function.

    Any tasks associated with this worker are explicitly canceled.

    :param name:            The name of the worker you wish to delete.
    :type  name:            basestring
    :param normal_shutdown: True if the worker shutdown normally, False otherwise.  Defaults to
                            False.
    :type normal_shutdown:  bool
    """
    if normal_shutdown is False:
        msg = _('The worker named %(name)s is missing. Canceling the tasks in its queue.')
        msg = msg % {'name': name}
        _logger.error(msg)
    else:
        msg = _("Cleaning up shutdown worker '%s'.") % name
        _logger.info(msg)

    # Delete the worker document
    Worker.objects(name=name).delete()

    # Delete all reserved_resource documents for the worker
    ReservedResource.objects(worker_name=name).delete()

    # If the worker is a resource manager, we also need to delete the associated lock
    if name.startswith(RESOURCE_MANAGER_WORKER_NAME):
        ResourceManagerLock.objects(name=name).delete()

    # If the worker is a scheduler, we also need to delete the associated lock
    if name.startswith(SCHEDULER_WORKER_NAME):
        CeleryBeatLock.objects(name=name).delete()

    # Cancel all of the tasks that were assigned to this worker's queue
    for task_status in TaskStatus.objects(worker_name=name,
                                          state__in=constants.CALL_INCOMPLETE_STATES):
        cancel(task_status['task_id'], revoke_task=False)


@task(base=PulpTask)
def _release_resource(task_id):
    """
    Do not queue this task yourself. It will be used automatically when your task is dispatched by
    the _queue_reserved_task task.

    When a resource-reserving task is complete, this method releases the resource by removing the
    ReservedResource object by UUID.

    :param task_id: The UUID of the task that requested the reservation
    :type  task_id: basestring
    """
    running_task_qs = TaskStatus.objects.filter(task_id=task_id, state=constants.CALL_RUNNING_STATE)
    for running_task in running_task_qs:
        new_task = Task()
        exception = PulpCodedException(error_codes.PLP0049, task_id=task_id)

        class MyEinfo(object):
            traceback = None

        new_task.on_failure(exception, task_id, (), {}, MyEinfo)
    ReservedResource.objects(task_id=task_id).delete()


class TaskResult(object):
    """
    The TaskResult object is used for returning errors and spawned tasks that do not affect the
    primary status of the task.

    Errors that don't affect the current task status might be related to secondary actions
    where the primary action of the async-task was successful

    Spawned tasks are items such as the individual tasks for updating the bindings on
    each consumer when a repo distributor is updated.
    """

    def __init__(self, result=None, error=None, spawned_tasks=None):
        """
        :param result: The return value from the task
        :type result: dict
        :param error: The PulpException for the error & sub-errors that occured
        :type error: pulp.server.exception.PulpException
        :param spawned_tasks: A list of task status objects for tasks that were created by this
                              task and are tracked through the pulp database.
                              Alternately an AsyncResult, or the task_id of the task created.
        :type spawned_tasks: list of TaskStatus, AsyncResult, or str objects
        """
        self.return_value = result
        self.error = error
        self.spawned_tasks = []
        if spawned_tasks:
            for spawned_task in spawned_tasks:
                if isinstance(spawned_task, dict):
                    self.spawned_tasks.append({'task_id': spawned_task.get('task_id')})
                elif isinstance(spawned_task, AsyncResult):
                    self.spawned_tasks.append({'task_id': spawned_task.id})
                elif isinstance(spawned_task, TaskStatus):
                    self.spawned_tasks.append({'task_id': spawned_task.task_id})
                else:  # This should be a string
                    self.spawned_tasks.append({'task_id': spawned_task})

    @classmethod
    def from_async_result(cls, async_result):
        """
        Create a TaskResult object from a celery async_result type

        :param async_result: The result object to use as a base
        :type async_result: celery.result.AsyncResult
        :returns: a TaskResult containing the async task in it's spawned_tasks list
        :rtype: TaskResult
        """
        return cls(spawned_tasks=[{'task_id': async_result.id}])

    @classmethod
    def from_task_status_dict(cls, task_status):
        """
        Create a TaskResult object from a celery async_result type

        :param task_status: The dictionary representation of a TaskStatus
        :type task_status: dict
        :returns: a TaskResult containing the task in it's spawned_tasks lsit
        :rtype: TaskResult
        """
        return cls(spawned_tasks=[{'task_id': task_status.task_id}])

    def serialize(self):
        """
        Serialize the output to a dictionary
        """
        serialized_error = self.error
        if serialized_error:
            serialized_error = self.error.to_dict()
        data = {
            'result': self.return_value,
            'error': serialized_error,
            'spawned_tasks': self.spawned_tasks}
        return data


class ReservedTaskMixin(object):
    def _apply_async_inner(self, reservation, *args, **kwargs):
        """
         This method allows the caller to schedule the ReservedTask to run asynchronously just like
         Celery's apply_async(), while also locking named resource(s). No two tasks that claim the
         same named-resource(s) can execute concurrently.

         It can accept a list-of-strings, of the form 'resource-type:resource-id'. If only
         asked for one resource (ie, list-len == 1), then call _queue_reserved_task, otherwise
         let _queue_reserved_task_list do the deed.

         This does not dispatch the task directly, but instead promises to dispatch it later. If the
         agument 'is_list' is True, the desired task is encapsualted by a call to
         _queue_reserved_task_list; otherwise, by a call to _queue_reserved_task.

         See the docblock on _queue_reserved_task and _queue_reserved_task_list for more
         information.

         This method creates a TaskStatus as a placeholder for later updates. Pulp expects to poll
         on a task just after calling this method, so a TaskStatus entry needs to exist for it
         before it returns.

         For a list of parameters accepted by the *args and **kwargs parameters, please see the
         docblock for the apply_async() method.

         :param reservation:    A list-of-strings that identify a set of named resources,
                                guaranteeing that only one task reserving any resource-ids in this
                                list can happen at a time.
         :type  reservation:    list
         :param tags:           A list of tags (strings) to place onto the task, used for searching
                                for tasks by tag
         :type  tags:           list
         :param group_id:       The id to identify which group of tasks a task belongs to
         :type  group_id:       uuid.UUID
         :return:               An AsyncResult instance as returned by Celery's apply_async
         :rtype:                celery.result.AsyncResult
         """
        inner_task_id = str(uuid.uuid4())
        task_name = self.name
        tag_list = kwargs.get('tags', [])
        group_id = kwargs.get('group_id', None)

        # Create a new task status with the task id and tags.
        task_status = TaskStatus(task_id=inner_task_id, task_type=task_name,
                                 state=constants.CALL_WAITING_STATE, tags=tag_list,
                                 group_id=group_id)
        # To avoid the race condition where __call__ method below is called before
        # this change is propagated to all db nodes, using an 'upsert' here and setting
        # the task state to 'waiting' only on an insert.
        task_status.save_with_set_on_insert(fields_to_set_on_insert=['state', 'start_time'])
        try:
            # Decide what to call based on how many reservation(s) we are being asked to make
            if len(reservation) == 1:
                _queue_reserved_task.apply_async(
                    args=[task_name, inner_task_id, reservation[0], args, kwargs],
                    queue=RESOURCE_MANAGER_QUEUE
                )
            else:
                _queue_reserved_task_list.apply_async(
                    args=[task_name, inner_task_id, reservation, args, kwargs],
                    queue=RESOURCE_MANAGER_QUEUE
                )
        except Exception:
            TaskStatus.objects(task_id=task_status.task_id).update(state=constants.CALL_ERROR_STATE)
            raise

        return AsyncResult(inner_task_id)

    def apply_async_with_reservation_list(self, resource_tuples, *args, **kwargs):
        """
         This method allows the caller to schedule the ReservedTask to run asynchronously just like
         Celery's apply_async(), while also locking the list of named resources. It accepts a
         list of tuples of the form (resource-type,resource-id), and combines them to form a list
         of resource-ids namespaced by their resource-type.

         See _apply_async_inner for details.

         For a list of parameters accepted by the *args and **kwargs parameters, please see the
         docblock for the apply_async() method.

         :param resource_tuples:    A list of strings that identify a set of named resources,
                                    guaranteeing that only one task reserving any resource-ids in
                                    this list can happen at a time. Elements are expected to be
                                    of the form (resource-type, resource-id)
         :type  resource_tuples:    list
         :param tags:               A list of tags (strings) to place onto the task, used for
                                    searching for tasks by tag
         :type  tags:               list
         :param group_id:           The id to identify which group of tasks a task belongs to
         :type  group_id:           uuid.UUID
         :return:                   An AsyncResult instance as returned by Celery's apply_async
         :rtype:                    celery.result.AsyncResult
         """
        # Build the list of real-resource-ids by concatentating resource-type
        # with each resource-id incoming
        resource_id_list = [":".join((rtype, rid)) for (rtype, rid) in resource_tuples]
        return self._apply_async_inner(resource_id_list, *args, **kwargs)

    def apply_async_with_reservation(self, resource_type, resource_id, *args, **kwargs):
        """
        This method allows the caller to schedule the ReservedTask to run asynchronously just like
        Celery's apply_async(), while also locking a named resource. It accepts a resource-type and
        the id of a resource of that type, and combines them to form a resource-id.

        See _apply_async_inner for details.

        For a list of parameters accepted by the *args and **kwargs parameters, please see the
        docblock for the apply_async() method.

        :param resource_type: A string that identifies type of a resource
        :type resource_type:  basestring
        :param resource_id:   A string that identifies some named resource, guaranteeing that only
                              one task reserving this same string can happen at a time.
        :type  resource_id:   basestring
        :param tags:          A list of tags (strings) to place onto the task, used for searching
                              for tasks by tag
        :type  tags:          list
        :param group_id:      The id to identify which group of tasks a task belongs to
        :type  group_id:      uuid.UUID
        :return:              An AsyncResult instance as returned by Celery's apply_async
        :rtype:               celery.result.AsyncResult
        """
        # Form a resource_id for reservation by combining given resource type and id. This way,
        # two different resources having the same id will not block each other.
        rsrc = [":".join((resource_type, resource_id))]
        return self._apply_async_inner(rsrc, *args, **kwargs)


class Task(PulpTask, ReservedTaskMixin):
    """
    This is a custom Pulp subclass of the PulpTask class. It allows us to inject some custom
    behavior into each Pulp task, including management of resource locking.
    """
    # this tells celery to not automatically log tracebacks for these exceptions
    throws = (PulpCodedException,)

    def apply_async(self, *args, **kwargs):
        """
        A wrapper around the PulpTask apply_async method. It allows us to accept a few more
        parameters than Celery does for our own purposes, listed below. It also allows us
        to create and update task status which can be used to track status of this task
        during it's lifetime.

        :param queue:       The queue that the task has been placed into (optional, defaults to
                            the general Celery queue.)
        :type  queue:       basestring
        :param tags:        A list of tags (strings) to place onto the task, used for searching for
                            tasks by tag
        :type  tags:        list
        :param group_id:    The id that identifies which group of tasks a task belongs to
        :type group_id:     uuid.UUID
        :return:            An AsyncResult instance as returned by Celery's apply_async
        :rtype:             celery.result.AsyncResult
        """
        if celery_version.startswith('4'):
            routing_key = kwargs.get('routing_key',
                                     defaults.NAMESPACES['task']['default_routing_key'].default)
        else:
            routing_key = kwargs.get('routing_key',
                                     defaults.NAMESPACES['CELERY']['DEFAULT_ROUTING_KEY'].default)
        tag_list = kwargs.pop('tags', [])
        group_id = kwargs.pop('group_id', None)

        try:
            async_result = super(Task, self).apply_async(*args, **kwargs)
        except Exception:
            if 'task_id' in kwargs:
                TaskStatus.objects(task_id=kwargs['task_id']).update(
                    state=constants.CALL_ERROR_STATE
                )
            raise

        async_result.tags = tag_list

        # Create a new task status with the task id and tags.
        task_status = TaskStatus(
            task_id=async_result.id, task_type=self.name,
            state=constants.CALL_WAITING_STATE, worker_name=routing_key, tags=tag_list,
            group_id=group_id)
        # We're now racing with __call__, on_failure and on_success, any of which may
        # have completed by now. To avoid overwriting TaskStatus updates from those callbacks,
        # we'll do an upsert and only touch the fields listed below if we've inserted the object.
        task_status.save_with_set_on_insert(fields_to_set_on_insert=[
            'state', 'start_time', 'finish_time', 'result', 'error',
            'spawned_tasks', 'traceback'])
        return async_result

    def __call__(self, *args, **kwargs):
        """
        This overrides PulpTask's __call__() method. We use this method
        for task state tracking of Pulp tasks.
        """
        # Check task status and skip running the task if task state is 'canceled'.
        try:
            task_status = TaskStatus.objects.get(task_id=self.request.id)
        except DoesNotExist:
            task_status = None
        if task_status and task_status['state'] == constants.CALL_CANCELED_STATE:
            _logger.debug("Task cancel received for task-id : [%s]" % self.request.id)
            return
        # Update start_time and set the task state to 'running' for asynchronous tasks.
        # Also update the worker_name to cover cases where apply_async was called without
        # providing the worker name up-front. Skip updating status for eagerly executed tasks,
        # since we don't want to track synchronous tasks in our database.
        if not self.request.called_directly:
            now = datetime.now(dateutils.utc_tz())
            start_time = dateutils.format_iso8601_datetime(now)
            worker_name = self.request.hostname
            # Using 'upsert' to avoid a possible race condition described in the apply_async method
            # above.
            try:
                TaskStatus.objects(task_id=self.request.id).update_one(
                    set__state=constants.CALL_RUNNING_STATE,
                    set__start_time=start_time,
                    set__worker_name=worker_name,
                    upsert=True)
            except NotUniqueError:
                # manually retry the upsert. see https://jira.mongodb.org/browse/SERVER-14322
                TaskStatus.objects(task_id=self.request.id).update_one(
                    set__state=constants.CALL_RUNNING_STATE,
                    set__start_time=start_time,
                    set__worker_name=worker_name,
                    upsert=True)

        # Run the actual task
        _logger.debug("Running task : [%s]" % self.request.id)

        if config.getboolean('profiling', 'enabled') is True:
            self.pr = cProfile.Profile()
            self.pr.enable()

        return super(Task, self).__call__(*args, **kwargs)

    def on_success(self, retval, task_id, args, kwargs):
        """
        This overrides the success handler run by the worker when the task
        executes successfully. It updates state, finish_time and traceback
        of the relevant task status for asynchronous tasks. Skip updating status
        for synchronous tasks.

        :param retval:  The return value of the task.
        :param task_id: Unique id of the executed task.
        :param args:    Original arguments for the executed task.
        :param kwargs:  Original keyword arguments for the executed task.
        """
        _logger.debug("Task successful : [%s]" % task_id)
        if kwargs.get('scheduled_call_id') is not None:
            if not isinstance(retval, AsyncResult):
                _logger.info(_('resetting consecutive failure count for schedule %(id)s')
                             % {'id': kwargs['scheduled_call_id']})
                utils.reset_failure_count(kwargs['scheduled_call_id'])
        if not self.request.called_directly:
            now = datetime.now(dateutils.utc_tz())
            finish_time = dateutils.format_iso8601_datetime(now)
            task_status = TaskStatus.objects.get(task_id=task_id)
            task_status['finish_time'] = finish_time
            task_status['result'] = retval

            # Only set the state to finished if it's not already in a complete state. This is
            # important for when the task has been canceled, so we don't move the task from canceled
            # to finished.
            if task_status['state'] not in constants.CALL_COMPLETE_STATES:
                task_status['state'] = constants.CALL_FINISHED_STATE
            if isinstance(retval, TaskResult):
                task_status['result'] = retval.return_value
                if retval.error:
                    task_status['error'] = retval.error.to_dict()
                if retval.spawned_tasks:
                    task_list = []
                    for spawned_task in retval.spawned_tasks:
                        if isinstance(spawned_task, AsyncResult):
                            task_list.append(spawned_task.task_id)
                        elif isinstance(spawned_task, dict):
                            task_list.append(spawned_task['task_id'])
                    task_status['spawned_tasks'] = task_list
            if isinstance(retval, AsyncResult):
                task_status['spawned_tasks'] = [retval.task_id, ]
                task_status['result'] = None

            task_status.save()
            self._handle_cProfile(task_id)
            common_utils.delete_working_directory()

    def _handle_on_failure_cleanup(self, task_id, exc, einfo):
        now = datetime.now(dateutils.utc_tz())
        finish_time = dateutils.format_iso8601_datetime(now)
        task_status = TaskStatus.objects.get(task_id=task_id)
        task_status['state'] = constants.CALL_ERROR_STATE
        task_status['finish_time'] = finish_time
        task_status['traceback'] = einfo.traceback
        if not isinstance(exc, PulpException):
            exc = PulpException(str(exc))
        task_status['error'] = exc.to_dict()
        task_status.save()
        self._handle_cProfile(task_id)
        common_utils.delete_working_directory()


    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        This overrides the error handler run by the worker when the task fails.
        It updates state, finish_time and traceback of the relevant task status
        for asynchronous tasks. Skip updating status for synchronous tasks.

        :param exc:     The exception raised by the task.
        :param task_id: Unique id of the failed task.
        :param args:    Original arguments for the executed task.
        :param kwargs:  Original keyword arguments for the executed task.
        :param einfo:   celery's ExceptionInfo instance, containing serialized traceback.
        """
        if isinstance(exc, PulpCodedException):
            _logger.info(_('Task failed : [%(task_id)s] : %(msg)s') %
                         {'task_id': task_id, 'msg': str(exc)})
            _logger.debug(traceback.format_exc())
        else:
            _logger.info(_('Task failed : [%s]') % task_id)
            # celery will log the traceback, but if not save it for later
            original_formatted_traceback = traceback.format_exc()
        if kwargs.get('scheduled_call_id') is not None:
            utils.increment_failure_count(kwargs['scheduled_call_id'])
        try:
            called_directly = self.request.called_directly
        except Exception:  # a workaround for when celery's internal state is bad
            # celery won't log so we should
            _logger.debug(original_formatted_traceback)
            self._handle_on_failure_cleanup(task_id, exc, einfo)
            raise
        if not called_directly:
            self._handle_on_failure_cleanup(task_id, exc, einfo)


    def _handle_cProfile(self, task_id):
        """
        If cProfiling is enabled, stop the profiler and write out the data.

        :param task_id: the id of the task
        :type task_id: unicode
        """
        if config.getboolean('profiling', 'enabled') is True:
            self.pr.disable()
            profile_directory = config.get('profiling', 'directory')
            misc.mkdir(profile_directory, mode=0755)
            self.pr.dump_stats("%s/%s" % (profile_directory, task_id))


def cancel(task_id, revoke_task=True):
    """
    Cancel the task that is represented by the given task_id. This method cancels only the task
    with given task_id, not the spawned tasks. This also updates task's state to 'canceled'.

    :param task_id: The ID of the task you wish to cancel
    :type  task_id: basestring

    :param revoke_task: Whether to perform a celery revoke() on the task in edition to cancelling
                        Works around issue #2835 (https://pulp.plan.io/issues/2835)
    :type  revoke_task: bool

    :raises MissingResource: if a task with given task_id does not exist
    :raises PulpCodedException: if given task is already in a complete state
    """
    try:
        task_status = TaskStatus.objects.get(task_id=task_id)
    except DoesNotExist:
        raise MissingResource(task_id)

    if task_status['state'] in constants.CALL_COMPLETE_STATES:
        # If the task is already done, just stop
        msg = _('Task [%(task_id)s] already in a completed state: %(state)s')
        _logger.info(msg % {'task_id': task_id, 'state': task_status['state']})
        return

    if task_status['worker_name'] == 'agent':
        tag_dict = dict(
            [
                tags.parse_resource_tag(t) for t in task_status['tags'] if tags.is_resource_tag(t)
            ])
        agent_manager = managers.consumer_agent_manager()
        consumer_id = tag_dict.get(tags.RESOURCE_CONSUMER_TYPE)
        agent_manager.cancel_request(consumer_id, task_id)
    else:
        if revoke_task:
            controller.revoke(task_id, terminate=True)

    qs = TaskStatus.objects(task_id=task_id, state__nin=constants.CALL_COMPLETE_STATES)
    qs.update_one(set__state=constants.CALL_CANCELED_STATE)

    msg = _('Task canceled: %(task_id)s.')
    msg = msg % {'task_id': task_id}
    _logger.info(msg)


def get_current_task_id():
    """"
    Get the current task id from celery. If this is called outside of a running
    celery task it will return None

    :return: The ID of the currently running celery task or None if not in a task
    :rtype: str
    """
    if current_task and current_task.request and current_task.request.id:
        return current_task.request.id
    return None


def register_sigterm_handler(f, handler):
    """
    register_signal_handler is a method or function decorator. It will register a special signal
    handler for SIGTERM that will call handler() with no arguments if SIGTERM is received during the
    operation of f. Once f has completed, the signal handler will be restored to the handler that
    was in place before the method began.

    :param f:       The method or function that should be wrapped.
    :type  f:       instancemethod or function
    :param handler: The method or function that should be called when we receive SIGTERM.
                    handler will be called with no arguments.
    :type  handler: instancemethod or function
    :return:        A wrapped version of f that performs the signal registering and unregistering.
    :rtype:         instancemethod or function
    """
    def sigterm_handler(signal_number, stack_frame):
        """
        This is the signal handler that gets installed to handle SIGTERM. We don't wish to pass the
        signal_number or the stack_frame on to handler, so its only purpose is to avoid
        passing these arguments onward. It calls handler().

        :param signal_number: The signal that is being handled. Since we have registered for
                              SIGTERM, this will be signal.SIGTERM.
        :type  signal_number: int
        :param stack_frame:   The current execution stack frame
        :type  stack_frame:   None or frame
        """
        handler()

    def wrap_f(*args, **kwargs):
        """
        This function is a wrapper around f. It replaces the signal handler for SIGTERM with
        signerm_handler(), calls f, sets the SIGTERM handler back to what it was before, and then
        returns the return value from f.

        :param args:   The positional arguments to be passed to f
        :type  args:   tuple
        :param kwargs: The keyword arguments to be passed to f
        :type  kwargs: dict
        :return:       The return value from calling f
        :rtype:        Could be anything!
        """
        old_signal = signal.signal(signal.SIGTERM, sigterm_handler)
        try:
            return f(*args, **kwargs)
        finally:
            signal.signal(signal.SIGTERM, old_signal)

    return wrap_f
