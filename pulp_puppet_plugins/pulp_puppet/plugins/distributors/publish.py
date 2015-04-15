from datetime import datetime
from gettext import gettext as _
import copy
import gdbm
import hashlib
import json
import logging
import os
import shutil
import sys

from pulp.server.db.model.criteria import UnitAssociationCriteria

from pulp_puppet.common import constants
from pulp_puppet.common.constants import (STATE_FAILED, STATE_RUNNING, STATE_SUCCESS, STATE_SKIPPED)
from pulp_puppet.common.model import RepositoryMetadata, Module
from pulp_puppet.common.publish_progress import PublishProgressReport


_logger = logging.getLogger(__name__)


class PuppetModulePublishRun(object):
    """
    Used to perform a single publish of a puppet repository. This class will
    maintain state relevant to the run and should not be reused across runs.

    :ivar repo: repository being published
    :type repo: pulp.plugins.model.Repository

    :ivar publish_conduit: used to communicate with Pulp for this repo's run
    :type publish_conduit: pulp.plugins.conduits.repo_publish.RepoPublishConduit

    :ivar config: configuration to use for the run
    :type config: pulp.plugins.config.PluginCallConfiguration

    :ivar is_cancelled_call: call to check to see if the run has been cancelled
    :type is_cancelled_call: callable
    """

    def __init__(self, repo, publish_conduit, config, is_cancelled_call):
        self.repo = repo
        self.publish_conduit = publish_conduit
        self.config = config
        self.is_cancelled_call = is_cancelled_call

        self.progress_report = PublishProgressReport(self.publish_conduit)

    def perform_publish(self):
        """
        Performs the publish operation according to the configured state of the
        instance. The report to be sent back to Pulp is returned from this call.
        This call will make calls into the conduit's progress update as
        appropriate.

        This call executes serially. No threads are created by this call. It
        will not return until either a step fails or the entire publish is
        completed.

        :return: the report object to return to Pulp from the publish call
        :rtype:  pulp.plugins.model.PublishReport
        """
        _logger.info('Beginning publish for repository <%s>' % self.repo.id)

        try:
            modules = self._modules_step()

            if modules is not None:
                self._metadata_step(modules)
        finally:
            # One final update before finishing
            self.progress_report.update_progress()

            report = self.progress_report.build_final_report()
            return report

    def _modules_step(self):
        """
        Performs all of the necessary actions in the modules section of the
        publish. Calls in here should *only* update the modules-related steps
        in the progress report.

        :return: list of modules in the repository; None if the modules step
                 failed
        :rtype:  list of pulp.plugins.model.AssociatedUnit
        """
        self.progress_report.modules_state = STATE_RUNNING
        # Do not update here; the counts need to be set first by the
        # symlink_modules call.

        start_time = datetime.now()

        try:
            self._init_build_dir()
            modules = self._retrieve_repo_modules()
            self._symlink_modules(modules)
        except Exception, e:
            _logger.exception('Exception during modules step for repository <%s>' % self.repo.id)

            self.progress_report.modules_state = STATE_FAILED
            self.progress_report.modules_error_message = _('Error assembling modules')
            self.progress_report.modules_exception = e
            self.progress_report.modules_traceback = sys.exc_info()[2]

            end_time = datetime.now()
            duration = end_time - start_time
            self.progress_report.modules_execution_time = duration.seconds

            self.progress_report.update_progress()

            return None

        self.progress_report.modules_state = STATE_SUCCESS

        end_time = datetime.now()
        duration = end_time - start_time
        self.progress_report.modules_execution_time = duration.seconds

        self.progress_report.update_progress()

        return modules

    def _metadata_step(self, modules):
        """
        Performs all of the necessary actions in the metadata section of the
        publish. Calls in here should *only* update the metadata-related steps
        in the progress report.

        :type modules: list of pulp.plugins.model.AssociatedUnit
        """
        self.progress_report.metadata_state = STATE_RUNNING
        self.progress_report.update_progress()

        start_time = datetime.now()

        try:
            self._generate_metadata(modules)
            self._generate_dependency_data(modules)
            self._copy_to_published()
            self._cleanup_build_dir()
        except Exception, e:
            _logger.exception('Exception during metadata generation step for repository <%s>' % self.repo.id)
            self.progress_report.metadata_state = STATE_FAILED
            self.progress_report.metadata_error_message = _('Error generating repository metadata')
            self.progress_report.metadata_exception = e
            self.progress_report.metadata_traceback = sys.exc_info()[2]

            end_time = datetime.now()
            duration = end_time - start_time
            self.progress_report.metadata_execution_time = duration.seconds

            self.progress_report.update_progress()

            return

        self.progress_report.metadata_state = STATE_SUCCESS

        end_time = datetime.now()
        duration = end_time - start_time
        self.progress_report.metadata_execution_time = duration.seconds

        self.progress_report.update_progress()

    # -- publishing steps -----------------------------------------------------

    def _init_build_dir(self):
        """
        Initializes the directory in which the repository will be assembled
        prior to making it live. If this directory already exists from a
        previous partial run, it will be deleted.
        """
        _logger.info('Initializing build directory for repository <%s>' % self.repo.id)

        build_dir = self._build_dir()
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)

        os.makedirs(build_dir)

    def _cleanup_build_dir(self):
        """
        Deletes the build directory after a successful publish.
        """
        _logger.info('Cleaning up build directory for repository <%s>' % self.repo.id)

        build_dir = self._build_dir()
        shutil.rmtree(build_dir)

    def _retrieve_repo_modules(self):
        """
        Retrieves all modules in the repository.

        :return: list of modules in the repository; empty list if there are none
        :rtype:  list of pulp.plugins.model.AssociatedUnit
        """
        criteria = UnitAssociationCriteria(type_ids=[constants.TYPE_PUPPET_MODULE])
        all_units = self.publish_conduit.get_units(criteria=criteria)
        return all_units

    def _symlink_modules(self, modules):
        """
        Creates the appropriate symlinks from the location in Pulp where the
        module is stored to the build directory. The structure created by
        this call will match the expected structure of how the repository will
        be served.

        :type modules: list of pulp.plugins.model.AssociatedUnit
        """
        _logger.info('Creating symlinks for modules in repository <%s>' % self.repo.id)

        build_dir = self._build_dir()

        self.progress_report.modules_total_count = len(modules)
        self.progress_report.modules_finished_count = 0
        self.progress_report.modules_error_count = 0
        self.progress_report.update_progress()

        for module in modules:
            served_relative_path = self._build_relative_path(module)
            symlink_path = os.path.join(build_dir, served_relative_path)
            symlink_dir = os.path.split(symlink_path)[0]

            try:
                if not os.path.exists(symlink_dir):
                    os.makedirs(symlink_dir)
                os.symlink(module.storage_path, symlink_path)
                self.progress_report.modules_finished_count += 1
            except Exception:
                self.progress_report.add_failed_module(module, sys.exc_info()[2])

            self.progress_report.update_progress()

    def _build_relative_path(self, module):
        """
        build a relative path from the repository root to the module

        :param module:  puppet module
        :type  module:  pulp.plugins.model.AssociatedUnit
        :return:    relative path to module file
        :rtype:     str
        """
        subs = (module.unit_key['author'][0], module.unit_key['author'])
        served_relative_path = constants.HOSTED_MODULE_FILE_RELATIVE_PATH % subs
        return os.path.join(served_relative_path, os.path.basename(module.storage_path))

    @property
    def _repo_path(self):
        """
        build an absolute path (URL component) to the repo being published

        :return:    absolute path (URL component) to the repo being published
        :rtype:     str
        """
        base_path = self.config.get(constants.CONFIG_ABSOLUTE_PATH, constants.DEFAULT_ABSOLUTE_PATH)
        return os.path.join(base_path, self.repo.id)

    def _generate_metadata(self, modules):
        """
        Generates the repository metadata document for all modules in the

        :type modules: list of pulp.plugins.model.AssociatedUnit
        """
        _logger.info('Generating metadata for repository <%s>' % self.repo.id)

        # Convert the Pulp data types into the local model
        metadata = RepositoryMetadata()

        for m in modules:
            combined = copy.copy(m.unit_key)
            combined.update(m.metadata)
            module = Module.from_dict(combined)
            metadata.modules.append(module)

        # Write the JSON representation of the metadata to the repository
        json_metadata = metadata.to_json()
        build_dir = self._build_dir()
        metadata_file = os.path.join(build_dir, constants.REPO_METADATA_FILENAME)

        f = open(metadata_file, 'w')
        f.write(json_metadata)
        f.close()

    def _generate_dependency_data(self, modules):
        """
        generate the dependency metadata that is required to provide the API
        that the "puppet module" tool uses. Store the metadata in a gdbm
        database at the root of the repo. Generating and storing it at publish
        time means the API requests will always return results that are in-sync
        with the most recent publish and are not influenced by more recent
        changes to the repo or its contents.

        :type modules: list of pulp.plugins.model.AssociatedUnit
        """
        filename = os.path.join(self._build_dir(), constants.REPO_DEPDATA_FILENAME)
        _logger.debug('generating dependency metadata in file %s' % filename)
        # opens a new file for writing and overwrites any existing file
        db = gdbm.open(filename, 'n')
        try:
            for module in modules:
                version = module.unit_key['version']
                deps = module.metadata.get('dependencies', [])
                path = os.path.join(self._repo_path, self._build_relative_path(module))
                # calculate the checksum
                with open(module.storage_path) as file_handle:
                    file_hash = hashlib.md5()
                    while True:
                        content = file_handle.read(128)
                        if not content:
                            break
                        file_hash.update(content)
                    md5_sum = file_hash.hexdigest()
                value = {'file': path, 'version': version, 'dependencies': deps,
                         'file_md5': md5_sum}

                name = module.unit_key['name']
                author = module.unit_key['author']
                key = '%s/%s' % (author, name)

                # db is not a dictionary as assumed by flake8
                if db.has_key(key): # noqa
                    module_list = json.loads(db[key])
                else:
                    module_list = []
                module_list.append(value)
                db[key] = json.dumps(module_list)
        finally:
            db.close()

    def _copy_to_published(self):
        """
        Moves the built repository into the proper locations where it will be
        hosted. If a directory is found at the destination, it will be deleted
        first.
        """
        _logger.info('Making newly built repository live for repository <%s>' % self.repo.id)

        build_dir = self._build_dir()

        # Remove the existing repository if it's found. It will either
        # remain deleted if the configuration changed and it shouldn't be
        # served, or it will be replaced with the newly built one.

        # -- HTTP --------
        proto_dir = self.config.get(constants.CONFIG_HTTP_DIR)
        repo_dest_dir = os.path.join(proto_dir, self.repo.id)

        unpublish(proto_dir, self.repo)

        should_serve = self.config.get_boolean(constants.CONFIG_SERVE_HTTP)
        if should_serve:
            shutil.copytree(build_dir, repo_dest_dir, symlinks=True)
            self.progress_report.publish_http = STATE_SUCCESS
        else:
            self.progress_report.publish_http = STATE_SKIPPED

        self.progress_report.update_progress()

        # -- HTTPS --------
        proto_dir = self.config.get(constants.CONFIG_HTTPS_DIR)
        repo_dest_dir = os.path.join(proto_dir, self.repo.id)

        unpublish(proto_dir, self.repo)

        should_serve = self.config.get_boolean(constants.CONFIG_SERVE_HTTPS)
        if should_serve:
            shutil.copytree(build_dir, repo_dest_dir, symlinks=True)
            self.progress_report.publish_https = STATE_SUCCESS
        else:
            self.progress_report.publish_https = STATE_SKIPPED

        self.progress_report.update_progress()

    # -- helpers --------------------------------------------------------------

    def _build_dir(self):
        """
        Returns the location in which the repository should be assembled during
        the publish process. This directory will be located under the repository
        working directory and be scoped to the repository being published.

        :return: full path to the directory in which to build the repo
        :rtype:  str
        """
        build_dir = os.path.join(self.repo.working_dir, 'build', self.repo.id)
        return build_dir


def unpublish_repo(repo, config):
    """
    Performs all clean up required to stop hosting the provided repository.
    If the repository was never published, this call has no effect.

    :param repo: repository instance given to the plugin by Pulp
    :type  repo: pulp.plugins.model.Repository
    :param config: config instance passed into the plugin by Pulp
    :type  config: pulp.plugins.config.PluginCallConfiguration
    :return:
    """

    for proto_key in (constants.CONFIG_HTTP_DIR, constants.CONFIG_HTTPS_DIR):
        proto_dir = config.get(proto_key)
        unpublish(proto_dir, repo)


def unpublish(protocol_directory, repo):
    """
    Unpublishes the repository from the given protocol hosting directory.
    If the repository was never published, this call has no effect.

    :param protocol_directory: directory the repository was published to
    :type  protocol_directory: str
    :param repo: repository instance given to the plugin by Pulp
    :type  repo: pulp.plugins.model.Repository
    """
    repo_dest_dir = os.path.join(protocol_directory, repo.id)

    if os.path.exists(repo_dest_dir):
        shutil.rmtree(repo_dest_dir)
