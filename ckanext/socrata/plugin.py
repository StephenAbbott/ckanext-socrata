from __future__ import unicode_literals

import json
from urlparse import urlparse

import requests
from simplejson.scanner import JSONDecodeError

from ckan import model
from ckan.lib.munge import munge_title_to_name, munge_tag
from ckan.plugins.core import implements
import ckan.plugins.toolkit as toolkit
from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.harvesters.base import HarvesterBase
from ckanext.harvest.model import HarvestObject

import logging
log = logging.getLogger(__name__)

BASE_API_ENDPOINT = "https://api.us.socrata.com/api/catalog/v1"
DOWNLOAD_ENDPOINT_TEMPLATE = \
    "https://{domain}/api/views/{resource_id}/rows.csv?accessType=DOWNLOAD"


class SocrataHarvester(HarvesterBase):
    '''
    A CKAN Harvester for Socrata Data catalogues.
    '''
    implements(IHarvester)

    def _delete_dataset(self, id):
        base_context = {
            'model': model,
            'session': model.Session,
            'user': self._get_user_name(),
            'ignore_auth': True
        }
        # Delete package
        toolkit.get_action('package_delete')(base_context, {'id': id})
        log.info('Deleted package with id {0}'.format(id))

    def _get_existing_dataset(self, guid):
        '''
        Check if a dataset with an `identifier` extra already exists.

        Return a dict in `package_show` format.
        '''
        datasets = model.Session.query(model.Package.id) \
            .join(model.PackageExtra) \
            .filter(model.PackageExtra.key == 'identifier') \
            .filter(model.PackageExtra.value == guid) \
            .filter(model.Package.state == 'active') \
            .all()

        if not datasets:
            return None
        elif len(datasets) > 1:
            log.error('Found more than one dataset with the same guid: {0}'
                      .format(guid))

        return toolkit.get_action('package_show')({}, {'id': datasets[0][0]})

    def _get_object_extra(self, harvest_object, key):
        '''
        Helper function for retrieving the value from a harvest object extra,
        given the key
        '''
        for extra in harvest_object.extras:
            if extra.key == key:
                return extra.value
        return None

    def _build_package_dict(self, context, harvest_object):
        '''
        Build and return a package_dict suitable for use with CKAN
        `package_create` and `package_update`.
        '''

        # Local harvest source organization
        source_dataset = toolkit.get_action('package_show')(
            context.copy(),
            {'id': harvest_object.source.id}
        )
        local_org = source_dataset.get('owner_org')

        res = json.loads(harvest_object.content)

        package_dict = {
            'title': res['resource']['name'],
            'name': munge_title_to_name(res['resource']['name']),
            'url': res.get('permalink', ''),
            'notes': res['resource'].get('description', ''),
            'author': res['resource']['attribution'],
            'tags': [],
            'extras': [],
            'identifier': res['resource']['id'],
            'owner_org': local_org,
            'resources': []
        }

        # Add tags
        package_dict['tags'] = \
            [{'name': munge_tag(t)}
             for t in res['classification'].get('tags', [])
             + res['classification'].get('domain_tags', [])]

        # Add domain_metadata to extras
        package_dict['extras'].extend(res['classification']
                                      .get('domain_metadata', []))

        # Add harvester details
        package_dict.update({
            'harvest_source_id': harvest_object.job.source.id,
            'harvest_source_url': harvest_object.job.source.url.strip('/'),
            'harvest_source_title': harvest_object.job.source.title,
            # 'harvest_job_id': harvest_object.job.id,
            # 'harvest_object_id': harvest_object.id
        })

        # Add provenance
        if res['resource'].get('provenance', False):
            package_dict['provenance'] = res['resource']['provenance']

        # Resources
        package_dict['resources'] = [{
            'url': DOWNLOAD_ENDPOINT_TEMPLATE.format(
                domain=urlparse(harvest_object.source.url).hostname,
                resource_id=res['resource']['id']),
            'format': 'CSV'
        }]

        return package_dict

    def info(self):
        return {
            'name': 'socrata',
            'title': 'Socrata',
            'description': 'Harvests from Socrata data catalogues'
        }

    def gather_stage(self, harvest_job):
        '''
        Gather dataset content from Socrate and create HarvestObjects for each
        dataset.

        :param harvest_job: HarvestJob object
        :returns: A list of HarvestObject ids
        '''

        def _request_datasets_from_socrata(domain, limit=100, offset=0):
            api_request_url = \
                '{0}?domains={1}&search_context={1}' \
                '&only=datasets&limit={2}&offset={3}' \
                .format(BASE_API_ENDPOINT, domain, limit, offset)
            log.debug('Requesting {}'.format(api_request_url))
            api_response = requests.get(api_request_url)

            try:
                api_json = api_response.json()
            except JSONDecodeError:
                self._save_gather_error(
                    'Gather error: Invalid response from {}'
                    .format(api_request_url),
                    harvest_job)
                return None

            if 'error' in api_json:
                self._save_gather_error('Gather error: {}'
                                        .format(api_json['error']),
                                        harvest_job)
                return None

            return api_json['results']

        def _page_datasets(domain, batch_number):
            '''Request datasets by page until an empty array is returned'''
            current_offset = 0
            while True:
                datasets = \
                    _request_datasets_from_socrata(domain, batch_number,
                                                   current_offset)
                if datasets is None or len(datasets) == 0:
                    raise StopIteration
                current_offset = current_offset + batch_number
                for dataset in datasets:
                    yield dataset

        def _make_harvest_objs(datasets):
            '''Create HarvestObject with Socrata dataset content.'''
            obj_ids = []
            for d in datasets:
                log.debug('Creating HarvestObject for {} {}'
                          .format(d['resource']['name'],
                                  d['resource']['id']))
                obj = HarvestObject(guid=d['resource']['id'],
                                    job=harvest_job,
                                    content=json.dumps(d))
                obj.save()
                obj_ids.append(obj.id)
            return obj_ids

        log.debug('In SocrataHarvester gather_stage (%s)',
                  harvest_job.source.url)

        domain = urlparse(harvest_job.source.url).hostname

        return _make_harvest_objs(_page_datasets(domain, 100))

    def fetch_stage(self, harvest_object):
        '''
        No fetch required, all package data obtained from gather stage.
        '''
        return True

    def import_stage(self, harvest_object):
        '''

        '''
        log.debug('In SocrataHarvester import_stage')

        base_context = {
            'model': model,
            'session': model.Session,
            'user': self._get_user_name(),
            'ignore_auth': True
        }

        # status = self._get_object_extra(harvest_object, 'status')
        # if status == 'delete':
        #     # Delete package
        #     toolkit.get_action('package_delete')(
        #         base_context, {'id': harvest_object.package_id})
        #     log.info('Deleted package {0} with guid {1}'
        #              .format(harvest_object.package_id, harvest_object.guid))
        #     return True

        if not harvest_object:
            log.error('No harvest object received')
            return False

        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' %
                                    harvest_object.id,
                                    harvest_object, 'Import')
            return False

        # Get the last harvested object (if any)
        previous_object = model.Session.query(HarvestObject) \
            .filter(HarvestObject.guid == harvest_object.guid) \
            .filter(HarvestObject.current is True) \
            .first()

        # Flag previous object as not current anymore
        if previous_object:
            previous_object.current = False
            previous_object.add()

        # Flag this object as the current one
        harvest_object.current = True
        harvest_object.add()

        res = json.loads(harvest_object.content)

        # Check if a dataset with the same guid exists
        existing_dataset = self._get_existing_dataset(harvest_object.guid)

        # Delete package (dev testing)
        # if existing_dataset:
        #     self._delete_dataset(existing_dataset['id'])
        # return False

        package_dict = self._build_package_dict(base_context, harvest_object)

        if existing_dataset:
            log.debug('Existing dataset {}'.format(res['resource']['id']))

            try:
                toolkit.get_action('package_update')(
                    base_context.copy(),
                    package_dict
                )
            except Exception as e:
                self._save_object_error('Error updating package for {}: {}'
                                        .format(harvest_object.id, e),
                                        harvest_object, 'Import')
                return False

        else:
            log.debug('New dataset {}'.format(res['resource']['id']))

            try:
                toolkit.get_action('package_create')(
                    base_context.copy(),
                    package_dict
                )
            except Exception as e:
                self._save_object_error('Error creating package for {}: {}'
                                        .format(harvest_object.id, e),
                                        harvest_object, 'Import')
                return False

        return True
