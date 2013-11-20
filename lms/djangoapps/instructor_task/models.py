"""
WE'RE USING MIGRATIONS!

If you make changes to this model, be sure to create an appropriate migration
file and check it in at the same time as your model changes. To do that,

1. Go to the edx-platform dir
2. ./manage.py schemamigration instructor_task --auto description_of_your_change
3. Add the migration file created in edx-platform/lms/djangoapps/instructor_task/migrations/


ASSUMPTIONS: modules have unique IDs, even across different module_types

"""
from cStringIO import StringIO
from gzip import GzipFile
from uuid import uuid4
import csv
import json
import hashlib
import os
import os.path
import urllib

from boto.s3.connection import S3Connection
from boto.s3.key import Key

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models, transaction


# define custom states used by InstructorTask
QUEUING = 'QUEUING'
PROGRESS = 'PROGRESS'


class InstructorTask(models.Model):
    """
    Stores information about background tasks that have been submitted to
    perform work by an instructor (or course staff).
    Examples include grading and rescoring.

    `task_type` identifies the kind of task being performed, e.g. rescoring.
    `course_id` uses the course run's unique id to identify the course.
    `task_key` stores relevant input arguments encoded into key value for testing to see
           if the task is already running (together with task_type and course_id).
    `task_input` stores input arguments as JSON-serialized dict, for reporting purposes.
        Examples include url of problem being rescored, id of student if only one student being rescored.

    `task_id` stores the id used by celery for the background task.
    `task_state` stores the last known state of the celery task
    `task_output` stores the output of the celery task.
        Format is a JSON-serialized dict.  Content varies by task_type and task_state.

    `requester` stores id of user who submitted the task
    `created` stores date that entry was first created
    `updated` stores date that entry was last modified
    """
    task_type = models.CharField(max_length=50, db_index=True)
    course_id = models.CharField(max_length=255, db_index=True)
    task_key = models.CharField(max_length=255, db_index=True)
    task_input = models.CharField(max_length=255)
    task_id = models.CharField(max_length=255, db_index=True)  # max_length from celery_taskmeta
    task_state = models.CharField(max_length=50, null=True, db_index=True)  # max_length from celery_taskmeta
    task_output = models.CharField(max_length=1024, null=True)
    requester = models.ForeignKey(User, db_index=True)
    created = models.DateTimeField(auto_now_add=True, null=True)
    updated = models.DateTimeField(auto_now=True)
    subtasks = models.TextField(blank=True)  # JSON dictionary

    def __repr__(self):
        return 'InstructorTask<%r>' % ({
            'task_type': self.task_type,
            'course_id': self.course_id,
            'task_input': self.task_input,
            'task_id': self.task_id,
            'task_state': self.task_state,
            'task_output': self.task_output,
        },)

    def __unicode__(self):
        return unicode(repr(self))

    @classmethod
    def create(cls, course_id, task_type, task_key, task_input, requester):
        """
        Create an instance of InstructorTask.

        The InstructorTask.save_now method makes sure the InstructorTask entry is committed.
        When called from any view that is wrapped by TransactionMiddleware,
        and thus in a "commit-on-success" transaction, an autocommit buried within here
        will cause any pending transaction to be committed by a successful
        save here.  Any future database operations will take place in a
        separate transaction.
        """
        # create the task_id here, and pass it into celery:
        task_id = str(uuid4())

        json_task_input = json.dumps(task_input)

        # check length of task_input, and return an exception if it's too long:
        if len(json_task_input) > 255:
            fmt = 'Task input longer than 255: "{input}" for "{task}" of "{course}"'
            msg = fmt.format(input=json_task_input, task=task_type, course=course_id)
            raise ValueError(msg)

        # create the task, then save it:
        instructor_task = cls(
            course_id=course_id,
            task_type=task_type,
            task_id=task_id,
            task_key=task_key,
            task_input=json_task_input,
            task_state=QUEUING,
            requester=requester
        )
        instructor_task.save_now()

        return instructor_task

    @transaction.autocommit
    def save_now(self):
        """
        Writes InstructorTask immediately, ensuring the transaction is committed.

        Autocommit annotation makes sure the database entry is committed.
        When called from any view that is wrapped by TransactionMiddleware,
        and thus in a "commit-on-success" transaction, this autocommit here
        will cause any pending transaction to be committed by a successful
        save here.  Any future database operations will take place in a
        separate transaction.
        """
        self.save()

    @staticmethod
    def create_output_for_success(returned_result):
        """
        Converts successful result to output format.

        Raises a ValueError exception if the output is too long.
        """
        # In future, there should be a check here that the resulting JSON
        # will fit in the column.  In the meantime, just return an exception.
        json_output = json.dumps(returned_result)
        if len(json_output) > 1023:
            raise ValueError("Length of task output is too long: {0}".format(json_output))
        return json_output

    @staticmethod
    def create_output_for_failure(exception, traceback_string):
        """
        Converts failed result information to output format.

        Traceback information is truncated or not included if it would result in an output string
        that would not fit in the database.  If the output is still too long, then the
        exception message is also truncated.

        Truncation is indicated by adding "..." to the end of the value.
        """
        tag = '...'
        task_progress = {'exception': type(exception).__name__, 'message': str(exception.message)}
        if traceback_string is not None:
            # truncate any traceback that goes into the InstructorTask model:
            task_progress['traceback'] = traceback_string
        json_output = json.dumps(task_progress)
        # if the resulting output is too long, then first shorten the
        # traceback, and then the message, until it fits.
        too_long = len(json_output) - 1023
        if too_long > 0:
            if traceback_string is not None:
                if too_long >= len(traceback_string) - len(tag):
                    # remove the traceback entry entirely (so no key or value)
                    del task_progress['traceback']
                    too_long -= (len(traceback_string) + len('traceback'))
                else:
                    # truncate the traceback:
                    task_progress['traceback'] = traceback_string[:-(too_long + len(tag))] + tag
                    too_long = 0
            if too_long > 0:
                # we need to shorten the message:
                task_progress['message'] = task_progress['message'][:-(too_long + len(tag))] + tag
            json_output = json.dumps(task_progress)
        return json_output

    @staticmethod
    def create_output_for_revoked():
        """Creates standard message to store in output format for revoked tasks."""
        return json.dumps({'message': 'Task revoked before running'})


class GradesStore(object):
    """
    Simple abstraction layer that can fetch and store CSV files for grades
    download. Should probably refactor later to create a GradesFile object that
    can simply be appended to for the sake of memory efficiency, rather than
    passing in the whole dataset. Doing that for now just because it's simpler.
    """
    @classmethod
    def from_config(cls):
        """
        Return one of the GradesStore subclasses depending on django
        configuration. Look at subclasses for expected configuration.
        """
        storage_type = settings.GRADES_DOWNLOAD.get("STORAGE_TYPE")
        if storage_type.lower() == "s3":
            return S3GradesStore.from_config()
        elif storage_type.lower() == "localfs":
            return LocalFSGradesStore.from_config()


class S3GradesStore(GradesStore):
    """

    """
    def __init__(self, bucket_name, root_path):
        self.root_path = root_path

        conn = S3Connection(
            settings.AWS_ACCESS_KEY_ID,
            settings.AWS_SECRET_ACCESS_KEY
        )
        self.bucket = conn.get_bucket(bucket_name)

    @classmethod
    def from_config(cls):
        return cls(
            settings.GRADES_DOWNLOAD['BUCKET'],
            settings.GRADES_DOWNLOAD['ROOT_PATH']
        )

    def key_for(self, course_id, filename):
        """Return the key we would use to store and retrive the data for the
        given filename."""
        hashed_course_id = hashlib.sha1(course_id)

        key = Key(self.bucket)
        key.key = "{}/{}/{}".format(
            self.root_path,
            hashed_course_id.hexdigest(),
            filename
        )

        return key

    def store(self, course_id, filename, buff):
        key = self.key_for(course_id, filename)

        data = buff.getvalue()
        key.size = len(data)
        key.content_encoding = "gzip"
        key.content_type = "text/csv"

        key.set_contents_from_string(
            data,
            headers={
                "Content-Encoding" : "gzip",
                "Content-Length" : len(data),
                "Content-Type" : "text/csv",
            }
        )

    def store_rows(self, course_id, filename, rows):
        """
        Given a course_id, filename, and rows (each row is an iterable of strings),
        write this data out.
        """
        output_buffer = StringIO()
        gzip_file = GzipFile(fileobj=output_buffer, mode="wb")
        csv.writer(gzip_file).writerows(rows)
        gzip_file.close()

        self.store(course_id, filename, output_buffer)

    def links_for(self, course_id):
        """
        For a given `course_id`, return a list of `(filename, url)` tuples. `url`
        can be plugged straight into an href
        """
        course_dir = self.key_for(course_id, '')
        return sorted(
            [
                (key.key.split("/")[-1], key.generate_url(expires_in=300))
                for key in self.bucket.list(prefix=course_dir.key)
            ],
            reverse=True
        )


class LocalFSGradesStore(GradesStore):
    """
    LocalFS implementation of a GradesStore. This is meant for debugging
    purposes and is *absolutely not for production use*. Use S3GradesStore for
    that.
    """
    def __init__(self, root_path):
        """
        Initialize with root_path where we're going to store our files. We
        will build a directory structure under this for each course.
        """
        self.root_path = root_path
        if not os.path.exists(root_path):
            os.makedirs(root_path)

    @classmethod
    def from_config(cls):
        """
        Generate an instance of this object from Django settings. It assumes
        that there is a dict in settings named GRADES_DOWNLOAD and that it has
        a ROOT_PATH that maps to an absolute file path that the web app has
        write permissions to. `LocalFSGradesStore` will create any intermediate
        directories as needed.
        """
        return cls(settings.GRADES_DOWNLOAD['ROOT_PATH'])

    def path_to(self, course_id, filename):
        """Return the full path to a given file for a given course."""
        return os.path.join(self.root_path, urllib.quote(course_id, safe=''), filename)

    def store(self, course_id, filename, buff):
        """
        Given the `course_id` and `filename`, store the contents of `buff` in
        that file. Overwrite anything that was there previously. `buff` is
        assumed to be a StringIO objecd (or anything that can flush its contents
        to string using `.getvalue()`).
        """
        full_path = self.path_to(course_id, filename)
        directory = os.path.dirname(full_path)
        if not os.path.exists(directory):
            os.mkdir(directory)

        with open(full_path, "wb") as f:
            f.write(buff.getvalue())

    def store_rows(self, course_id, filename, rows):
        """
        Given a course_id, filename, and rows (each row is an iterable of strings),
        write this data out.
        """
        output_buffer = StringIO()
        csv.writer(output_buffer).writerows(rows)
        self.store(course_id, filename, output_buffer)

    def links_for(self, course_id):
        """
        For a given `course_id`, return a list of `(filename, url)` tuples. `url`
        can be plugged straight into an href
        """
        course_dir = self.path_to(course_id, '')
        if not os.path.exists(course_dir):
            return []
        return sorted(
            [
                (filename, ("file://" + urllib.quote(os.path.join(course_dir, filename))))
                for filename in os.listdir(course_dir)
            ],
            reverse=True
        )