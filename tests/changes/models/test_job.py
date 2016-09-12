from __future__ import absolute_import

import mock

from flask import current_app

from changes.models.command import CommandType
from changes.models.jobplan import JobPlan
from changes.testutils import TestCase
from changes.utils.bazel_setup import COLLECT_BAZEL_TARGETS


class AutogeneratedJobTest(TestCase):
    def _create_job_and_jobplan(self):
        current_app.config['APT_SPEC'] = 'deb http://example.com/debian distribution component1'
        current_app.config['ENCAP_RSYNC_URL'] = 'rsync://example.com/encap/'
        current_app.config['BAZEL_APT_PKGS'] = ['bazel']
        current_app.config['BAZEL_ROOT_PATH'] = '/bazel/root/path'

        project = self.create_project()
        plan = self.create_plan(project)
        option = self.create_option(
            item_id=plan.id,
            name='bazel.autogenerate',
            value='1',
        )
        build = self.create_build(project)
        job = self.create_job(build)
        jobplan = self.create_job_plan(job, plan)
        return job

    @mock.patch('changes.models.project.Project.get_config')
    def test_autogenerated_commands(self, get_config):
        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.dependencies': {
                'encap': [
                    'package1',
                    'pkg-2',
                ]
            },
            'bazel.exclude-tags': [],
            'bazel.cpus': 4,           # Default
            'bazel.mem': 8192,         # Default
            'bazel.max-executors': 1,  # Default
        }

        with mock.patch('changes.utils.bazel_setup.COLLECT_BAZEL_TARGETS') as mock_collect_bazel_target_command:
            mock_collect_bazel_target_command.format.return_value = 'test script'
            _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)

        bazel_setup_expected = """#!/bin/bash -eux
# Clean up any existing apt sources
sudo rm -rf /etc/apt/sources.list.d
# Overwrite apt sources
echo "deb http://example.com/debian distribution component1" | sudo tee /etc/apt/sources.list

# apt-get update, and try again if it fails first time
sudo apt-get -y update || sudo apt-get -y update
sudo apt-get install -y --force-yes bazel

/usr/bin/bazel --nomaster_blazerc --blazerc=/dev/null --output_user_root=/bazel/root/path --batch version
""".strip()

        sync_encap_expected = """
sudo mkdir -p /usr/local/encap/
sudo /usr/bin/rsync -a --delete rsync://example.com/encap/package1 /usr/local/encap/
sudo /usr/bin/rsync -a --delete rsync://example.com/encap/pkg-2 /usr/local/encap/
""".strip()

        assert len(implementation.commands) == 3

        assert implementation.max_executors == 1

        assert implementation.artifacts == []
        assert implementation.artifact_suffix == '.bazel'

        assert implementation.commands[0].type == CommandType.setup
        assert implementation.commands[0].script == bazel_setup_expected

        assert implementation.commands[1].type == CommandType.setup
        assert implementation.commands[1].script == sync_encap_expected

        assert implementation.commands[2].type == CommandType.collect_bazel_targets
        assert implementation.commands[2].script == 'test script'

        kwargs = dict(
            apt_spec='deb http://example.com/debian distribution component1',
            bazel_apt_pkgs='bazel',
            bazel_root='/bazel/root/path',
            bazel_targets='//aa/bb/cc/...,//aa/abc/...',
            script=mock.ANY,  # this one is really not worth checking
            bazel_exclude_tags='',
            max_jobs=8,
        )
        mock_collect_bazel_target_command.format.assert_called_once_with(**kwargs)

        # make sure string formatting apply cleanly
        _, kwargs = mock_collect_bazel_target_command.format.call_args
        COLLECT_BAZEL_TARGETS.format(**kwargs)

    @mock.patch('changes.models.project.Project.get_config')
    def test_autogenerated_commands_with_exclusions(self, get_config):
        get_config.return_value = {
            'bazel.targets': [
                '//foo/bar/baz/...',
                '//bar/bax/...',
            ],
            'bazel.exclude-tags': [
                'flaky',
                'another_tag',
            ],
            'bazel.cpus': 2,
            'bazel.mem': 1234,
            'bazel.max-executors': 3,
        }

        with mock.patch('changes.utils.bazel_setup.COLLECT_BAZEL_TARGETS') as mock_collect_bazel_target_command:
            mock_collect_bazel_target_command.format.return_value = 'test script'
            _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)

        assert implementation.max_executors == 3

        assert implementation.artifacts == []
        assert implementation.artifact_suffix == '.bazel'

        assert implementation.resources['cpus'] == 2
        assert implementation.resources['mem'] == 1234

        assert len(implementation.commands) == 3

        assert implementation.commands[0].type == CommandType.setup
        assert implementation.commands[1].type == CommandType.setup
        assert implementation.commands[2].type == CommandType.collect_bazel_targets
        assert implementation.commands[2].script == 'test script'

        kwargs = dict(
            apt_spec='deb http://example.com/debian distribution component1',
            bazel_apt_pkgs='bazel',
            bazel_root='/bazel/root/path',
            bazel_targets='//foo/bar/baz/...,//bar/bax/...',
            script=mock.ANY,  # this one is really not worth checking
            bazel_exclude_tags='flaky,another_tag',
            max_jobs=4,
        )
        mock_collect_bazel_target_command.format.assert_called_once_with(**kwargs)

        # make sure string formatting apply cleanly
        _, kwargs = mock_collect_bazel_target_command.format.call_args
        COLLECT_BAZEL_TARGETS.format(**kwargs)

    @mock.patch('changes.models.project.Project.get_config')
    def test_invalid_cpus(self, get_config):
        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.exclude-tags': [],
            'bazel.cpus': 0,           # 0 CPUs is not valid
            'bazel.mem': 8192,
            'bazel.max-executors': 1,
        }

        _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)
        assert implementation is None

        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.exclude-tags': [],
            'bazel.cpus': 2,           # Too many
            'bazel.mem': 8192,
            'bazel.max-executors': 1,
        }

        current_app.config['MAX_CPUS_PER_EXECUTOR'] = 1
        _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)
        assert implementation is None

    @mock.patch('changes.models.project.Project.get_config')
    def test_invalid_mems(self, get_config):
        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.exclude-tags': [],
            'bazel.cpus': 1,
            'bazel.mem': 1025,         # Too high
            'bazel.max-executors': 1,
        }

        current_app.config['MIN_MEM_MB_PER_EXECUTOR'] = 1
        current_app.config['MAX_MEM_MB_PER_EXECUTOR'] = 10
        _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)

        assert implementation is None

        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.exclude-tags': [],
            'bazel.cpus': 1,
            'bazel.mem': 1025,         # Too low
            'bazel.max-executors': 1,
        }

        current_app.config['MIN_MEM_MB_PER_EXECUTOR'] = 2000
        current_app.config['MAX_MEM_MB_PER_EXECUTOR'] = 3000
        _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)

        assert implementation is None

    @mock.patch('changes.models.project.Project.get_config')
    def test_invalid_num_executors(self, get_config):
        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.exclude-tags': [],
            'bazel.cpus': 1,
            'bazel.mem': 1234,
            'bazel.max-executors': 0,  # invalid
        }

        _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)

        assert implementation is None

        get_config.return_value = {
            'bazel.targets': [
                '//aa/bb/cc/...',
                '//aa/abc/...',
            ],
            'bazel.exclude-tags': [],
            'bazel.cpus': 1,
            'bazel.mem': 1234,
            'bazel.max-executors': 11,  # too high
        }

        _, implementation = JobPlan.get_build_step_for_job(self._create_job_and_jobplan().id)

        assert implementation is None
