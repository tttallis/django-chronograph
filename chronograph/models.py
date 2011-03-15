from django.db import models
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.utils.timesince import timeuntil
from django.utils.translation import ungettext, ugettext, ugettext_lazy as _
from django.template import loader, Context
from django.conf import settings
from django.utils.encoding import smart_str

import os
import sys
import traceback
import subprocess
import shlex

from datetime import datetime
from dateutil import rrule
from StringIO import StringIO

class JobManager(models.Manager):
    def due(self):
        """
        Returns a ``QuerySet`` of all jobs waiting to be run.
        """
        return self.filter(next_run__lte=datetime.now(), disabled=False, is_running=False)

# A lot of rrule stuff is from django-schedule
freqs = (   ("YEARLY", _("Yearly")),
            ("MONTHLY", _("Monthly")),
            ("WEEKLY", _("Weekly")),
            ("DAILY", _("Daily")),
            ("HOURLY", _("Hourly")),
            ("MINUTELY", _("Minutely")),
            ("SECONDLY", _("Secondly")))

class Job(models.Model):
    """
    A recurring ``django-admin`` command to be run.
    """
    name = models.CharField(_("name"), max_length=200)
    frequency = models.CharField(_("frequency"), choices=freqs, max_length=10, blank=True)
    params = models.TextField(_("params"), null=True, blank=True,
        help_text=_('Comma-separated list of <a href="http://labix.org/python-dateutil" target="_blank">rrule parameters</a>. e.g: interval:15'))
    command = models.CharField(_("command"), max_length=200,
        help_text=_("A valid django-admin command to run."), blank=True)
    shell_command = models.CharField(_("shell command"), max_length=255,
        help_text=_("A shell command."), blank=True)
    run_in_shell = models.BooleanField(default=False, help_text=_('This command needs to run within a shell?'))
    args = models.CharField(_("args"), max_length=200, blank=True,
        help_text=_("Space separated list; e.g: arg1 option1=True"))
    disabled = models.BooleanField(default=False, help_text=_('If checked this job will never run.'))
    next_run = models.DateTimeField(_("next run"), blank=True, null=True, help_text=_("If you don't set this it will be determined automatically"))
    last_run = models.DateTimeField(_("last run"), editable=False, blank=True, null=True)
    is_running = models.BooleanField(_("Running?"), default=False, editable=False)
    last_run_successful = models.BooleanField(default=True, blank=False, null=False, editable=False)
    info_subscribers = models.ManyToManyField(User, related_name='info_subscribers_set', blank=True)
    subscribers = models.ManyToManyField(User, related_name='error_subscribers_set', blank=True)

    objects = JobManager()

    class Meta:
        ordering = ('disabled', 'next_run',)

    def __unicode__(self):
        if self.disabled:
            return _(u"%(name)s - disabled") % {'name': self.name}
        return u"%s - %s" % (self.name, self.timeuntil)

    def save(self, *args, **kwargs):
        if not self.disabled:
            if not self.last_run:
                self.last_run = datetime.now()
            if not self.next_run and self.frequency != '':
                self.next_run = self.rrule.after(self.last_run)
        else:
            self.next_run = None

        super(Job, self).save(*args, **kwargs)

    def get_timeuntil(self):
        """
        Returns a string representing the time until the next
        time this Job will be run.
        """
        if self.disabled:
            return _('never (disabled)')
            
        if not self.next_run:
            return _('never (expired)')

        delta = self.next_run - datetime.now()
        if delta.days < 0:
            # The job is past due and should be run as soon as possible
            return _('due')
        elif delta.seconds < 60:
            # Adapted from django.utils.timesince
            count = lambda n: ungettext('second', 'seconds', n)
            return ugettext('%(number)d %(type)s') % {
                'number': delta.seconds,
                'type': count(delta.seconds)
            }

        return timeuntil(self.next_run)
    get_timeuntil.short_description = _('time until next run')
    timeuntil = property(get_timeuntil)

    def get_rrule(self):
        """
        Returns the rrule objects for this Job.
        """
        frequency = getattr(rrule, self.frequency)
        if frequency:
            return rrule.rrule(frequency, dtstart=self.last_run, **self.get_params())
        return None
    rrule = property(get_rrule)

    def get_params(self):
        """
        >>> job = Job(params = "count:1;bysecond:1;byminute:1,2,4,5")
        >>> job.get_params()
        {'count': 1, 'byminute': [1, 2, 4, 5], 'bysecond': 1}
        """
        if self.params is None:
            return {}
        params = self.params.split(';')
        param_dict = []
        for param in params:
            param = param.split(':')
            if len(param) == 2:
                param = (str(param[0]), [int(p) for p in param[1].split(',')])
                if len(param[1]) == 1:
                    param = (param[0], param[1][0])
                param_dict.append(param)
        return dict(param_dict)

    def get_args(self):
        """
        Processes the args and returns a tuple or (args, options) for passing to ``call_command``.
        """
        args = []
        options = {}
        for arg in self.args.split():
            if arg.find('=') > -1:
                key, value = arg.split('=')
                options[smart_str(key)] = smart_str(value)
            else:
                args.append(arg)
        return (args, options)

    def run(self, save=True):
        """
        Runs this ``Job``.  If ``save`` is ``True`` the dates (``last_run`` and ``next_run``)
        are updated.  If ``save`` is ``False`` the job simply gets run and nothing changes.

        A ``Log`` will be created if there is any output from either stdout or stderr.
        """
        run_date = datetime.now()
        self.is_running = True
        self.save()

        stdout_str, stderr_str = "", ""

        try:
            if self.shell_command:
                stdout_str, stderr_str = self.run_shell_command()
            else:
                stdout_str, stderr_str = self.run_management_command()
        finally:
            # If stderr was written the job is not successful
            self.last_run_successful = not bool(stderr_str)
            self.is_running = False
            self.save()

        if save:
            self.last_run = run_date
            if self.frequency != '':
                self.next_run = self.rrule.after(run_date)
            else:
                self.next_run = None
            self.save()

        end_date = datetime.now()

        # Create a log entry no matter what to see the last time the Job ran:
        log = Log.objects.create(
            job = self,
            run_date = run_date,
            end_date = end_date,
            stdout = stdout_str,
            stderr = stderr_str,
            success = self.last_run_successful,
        )
        
        # If there was any output to stderr, e-mail it to any error (defualt) subscribers.
        # We'll assume that if there was any error output, even if there was also info output
        # That an error exists and needs to be dealt with
        if stderr_str:
            log.email_subscribers()

        # Otherwise - if there was only output to stdout, e-mail it to any info subscribers
        elif stdout_str:
            log.email_subscribers(is_info=True)

    def run_management_command(self):
        """
        Runs a management command job
        """
        from django.core.management import call_command

        args, options = self.get_args()
        stdout = StringIO()
        stderr = StringIO()

        # Redirect output so that we can log it if there is any
        ostdout = sys.stdout
        ostderr = sys.stderr
        sys.stdout = stdout
        sys.stderr = stderr
        stdout_str, stderr_str, exception_str = "", "", ""

        try:
            call_command(self.command, *args, **options)
        except Exception, e:
            exception_str = self._get_exception_string(e, sys.exc_info())
            self.last_run_successful = False

        sys.stdout = ostdout
        sys.stderr = ostderr

        stdout_str = stdout.getvalue()
        stderr_str = stderr.getvalue()

        return stdout_str, stderr_str + exception_str

    def run_shell_command(self):
        """
        Returns the stdout and stderr of a command being run.
        """
        stdout_str, stderr_str = "", ""
        command = self.shell_command + ' ' + (self.args or '')
        if self.run_in_shell:
            command = _escape_shell_command(command)
        else:
            command = shlex.split(command.encode('ascii'))
        try:
            proc = subprocess.Popen(command,
                                    shell = bool(self.run_in_shell),
                                    stdout = subprocess.PIPE,
                                    stderr = subprocess.PIPE)

            stdout_str, stderr_str = proc.communicate()
            if proc.returncode:
                stderr_str += "\n\n*** Process ended with return code %d\n\n" % proc.returncode
            self.last_run_successful = not proc.returncode
        except Exception, e:
            stderr_str += self._get_exception_string(e, sys.exc_info())
            self.last_run_successful = False

        return stdout_str, stderr_str

    def _get_exception_string(self, e, exc_info):
        t = loader.get_template('chronograph/error_message.txt')
        c = Context({
                'exception': unicode(e),
                'traceback': ['\n'.join(traceback.format_exception(*exc_info))]
                })
        return t.render(c)




class Log(models.Model):
    """
    A record of stdout and stderr of a ``Job``.
    """
    job = models.ForeignKey(Job)
    run_date = models.DateTimeField()
    end_date = models.DateTimeField(null=True)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    success = models.BooleanField(default=True, editable=False)

    class Meta:
        ordering = ('-run_date',)

    def __unicode__(self):
        return u"%s" % self.job.name

    def get_duration(self):
        if self.end_date:
            return self.end_date - self.run_date;
        else:
            return None

    def email_subscribers(self, is_info=False):
        subscribers = []

        if is_info:
            subscriber_set = self.job.info_subscribers.all()
        else:
            subscriber_set = self.job.subscribers.all()

        for user in subscriber_set:
            subscribers.append('"%s" <%s>' % (user.get_full_name(), user.email))


        message_body = """
********************************************************************************
JOB NAME: %(job_name)s
RUN DATE: %(run_date)s
END DATE: %(end_date)s
SUCCESSFUL: %(success)s
********************************************************************************


********************************************************************************
INFORMATIONAL OUTPUT
********************************************************************************
%(info_output)s

********************************************************************************
ERROR OUTPUT
********************************************************************************
%(error_output)s
""" % { 'job_name': self.job.name,
        'run_date': self.run_date,
        'end_date': self.end_date,
        'success': self.success,
        'info_output': self.stdout,
        'error_output': self.stderr, }

        send_mail(
            from_email = '"%s" <%s>' % (settings.EMAIL_SENDER, settings.EMAIL_HOST_USER),
            subject = '%s' % self,
            recipient_list = subscribers,
            #essage = "Ouput:\n%s\nError output:\n%s" % (self.stdout, self.stderr)
            message = message_body
        )

def _escape_shell_command(command):
    for n in ('`', '$', '"'):
        command = command.replace(n, '\%s' % n)
    return command

# convenience function
def schedule_single_job(eta, command, argstring=''):
    now = datetime.now()
    if eta > now:
        return Job.objects.get_or_create(name=command, command=command, args=argstring, next_run=eta)
    return False