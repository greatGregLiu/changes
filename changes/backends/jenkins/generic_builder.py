from __future__ import absolute_import

from flask import current_app

from changes.config import db
from changes.models import FutureCommand, SnapshotImage
from changes.utils.http import build_uri

from .builder import JenkinsBuilder


class JenkinsGenericBuilder(JenkinsBuilder):
    def __init__(self, master_urls=None, setup_script='', teardown_script='',
                 reset_script='', diff_cluster=None, path='', workspace='',
                 *args, **kwargs):
        self.setup_script = setup_script
        self.script = kwargs.pop('script')
        self.teardown_script = teardown_script
        self.reset_script = reset_script
        self.cluster = kwargs.pop('cluster')
        self.diff_cluster = diff_cluster
        self.path = path
        self.workspace = workspace

        # See configuration for more details; by default, the default build type is
        # legacy which sets up no additional configuration.
        self.build_type = kwargs.pop('build_type',
            current_app.config['CHANGES_CLIENT_DEFAULT_BUILD_TYPE'])
        if self.build_type is None:
            self.build_type = current_app.config['CHANGES_CLIENT_DEFAULT_BUILD_TYPE']

        # we do this as early as possible in order to propagate the
        # error faster. The build description is simply the configuration
        # key'd by the build_type, documented in config.py
        self.build_desc = self.load_build_desc(self.build_type)

        if not master_urls:
            # if we haven't specified master urls, lets try to take the default
            # for this given cluster
            master_urls = current_app.config['JENKINS_CLUSTERS'].get(self.cluster)

        super(JenkinsGenericBuilder, self).__init__(master_urls, *args, **kwargs)

    def load_build_desc(self, build_type):
        build_desc = current_app.config['CHANGES_CLIENT_BUILD_TYPES'][build_type]
        self.validate_build_desc(build_type, build_desc)
        return build_desc

    # TODO validate configuration at start of application or use a linter to validate
    # configuration before pushing/deploying
    def validate_build_desc(self, build_type, build_desc):
        if build_desc.get('uses_client', False):
            if 'jenkins-command' not in build_desc:
                raise ValueError('[CHANGES_CLIENT_BUILD_TYPES INVALID] build type %s missing required key: jenkins-command' % build_type)
            if 'adapter' not in build_desc:
                raise ValueError('[CHANGES_CLIENT_BUILD_TYPES INVALID] build type %s missing required key: adapter' % build_type)

    # These three methods all describe which build specification,
    # setup, and teardown should be used to create a snapshot
    # build. In the generic builder, this is the same as a normal build,
    # but sharded builds need to override these with the shard equivalents
    # in order to create the correct snapshot.
    def get_snapshot_build_desc(self):
        return self.build_desc

    def get_snapshot_setup_script(self):
        return self.setup_script

    def get_snapshot_teardown_script(self):
        return self.teardown_script

    def get_expected_image(self, job_id):
        return db.session.query(
            SnapshotImage.id,
        ).filter(
            SnapshotImage.job_id == job_id,
        ).scalar()

    def get_job_parameters(self, job, changes_bid, setup_script=None,
            script=None, teardown_script=None, path=None):
        """
        Gets a list containing dictionaries, each with two keys - name and value.
        These key,value pairs correspond to the input variables in Jenkins.

        changes_bid is actually the jobstep id, and job is the current job.
        *_script and path override the corresponding fields of the current
        builder.
        """
        params = super(JenkinsGenericBuilder, self).get_job_parameters(
            job, changes_bid=changes_bid)

        if path is None:
            path = self.path

        if setup_script is None:
            setup_script = self.setup_script

        if script is None:
            script = self.script

        if teardown_script is None:
            teardown_script = self.teardown_script

        project = job.project
        repository = project.repository

        vcs = repository.get_vcs()
        if vcs:
            repo_url = vcs.remote_url
        else:
            repo_url = repository.url

        cluster = self.cluster
        is_diff = not job.source.is_commit()
        if is_diff and self.diff_cluster:
            cluster = self.diff_cluster

        snapshot_bucket = current_app.config.get('SNAPSHOT_S3_BUCKET', '') or ''

        default_pre = current_app.config.get('LXC_PRE_LAUNCH', '') or ''
        default_post = current_app.config.get('LXC_POST_LAUNCH', '') or ''
        default_release = current_app.config.get('LXC_RELEASE', 'trusty') or ''

        build_desc = self.build_desc

        expected_image = self.get_expected_image(job.id)

        # Setting script to be empty essentially forces nothing
        # but setup/teardown to be run, making a clean snapshot
        snapshot_id = ''
        if expected_image:
            snapshot_id = expected_image.hex
            # this is a no-op command in sh, essentially equivalent
            # to '' except it tells changes-client that we are
            # deliberately doing absolutely nothing
            script = ':'

            # sharded builds will have different setup/teardown/build_desc
            # scripts between shards and collector so we need to
            # use the shard ones
            build_desc = self.get_snapshot_build_desc()
            setup_script = self.get_snapshot_setup_script()
            teardown_script = self.get_snapshot_teardown_script()

        # CHANGES_BID, the jobstep id, is provided by superclass
        params.extend([
            {'name': 'CHANGES_PID', 'value': project.slug},
            {'name': 'REPO_URL', 'value': repo_url},
            {'name': 'SETUP_SCRIPT', 'value': setup_script},
            {'name': 'SCRIPT', 'value': script},
            {'name': 'TEARDOWN_SCRIPT', 'value': teardown_script},
            {'name': 'RESET_SCRIPT', 'value': self.reset_script},
            {'name': 'REPO_VCS', 'value': repository.backend.name},
            {'name': 'CLUSTER', 'value': cluster},
            {'name': 'WORK_PATH', 'value': path},
            {'name': 'C_WORKSPACE', 'value': self.workspace},
        ])
        if build_desc.get('uses_client', False):
            params.extend([
                {'name': 'JENKINS_COMMAND',
                 'value': build_desc['jenkins-command']},
                {'name': 'CHANGES_CLIENT_ADAPTER',
                 'value': build_desc['adapter']},
                {'name': 'CHANGES_CLIENT_SERVER',
                 'value': build_uri('/api/0')},
                {'name': 'CHANGES_CLIENT_SNAPSHOT_BUCKET',
                 'value': snapshot_bucket},
                {'name': 'CHANGES_CLIENT_SNAPSHOT_ID',
                 'value': snapshot_id},
                {'name': 'CHANGES_CLIENT_LXC_PRE_LAUNCH',
                 'value': self.build_desc.get('pre-launch', default_pre)},
                {'name': 'CHANGES_CLIENT_LXC_POST_LAUNCH',
                 'value': self.build_desc.get('post-launch', default_post)},
                {'name': 'CHANGES_CLIENT_LXC_RELEASE',
                 'value': self.build_desc.get('release', default_release)},
            ])

        return params

    def params_to_env(self, params):
        return {param['name']: param['value'] for param in params}

    def get_future_commands(self, params, commands):
        return map(lambda command: FutureCommand(command['script'],
                                    env=self.params_to_env(params)),
                   commands)

    def create_commands(self, jobstep, params):
        commands = self.build_desc.get('commands', [])

        index = 0
        for future_command in self.get_future_commands(params, commands):
            db.session.add(future_command.as_command(jobstep, index))
            index += 1

    def can_snapshot(self):
        return self.build_desc.get('can_snapshot', False)
