#!/usr/bin/env python
import json
import logging
import os
import requests
import sys
import urllib3
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from urllib.parse import urljoin, urlparse
import http.client as http_client

__verbose = False
__master_versions = {}
log_level = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(level=log_level)

# URLs to use when running the script from inside a DCOS cluster:

class DCOS_Janitor:
    def __init__(self, **kwargs):
        """
        Args:
            **kwargs:
        """
        self.protocol = kwargs.get('protocol', None) or os.getenv('PROTOCOL', 'https')
        dcos_leader_url = kwargs.get('leader_url', None) or os.getenv('LEADER_URL', '/master')
        master_path = kwargs.get('master_path', None) or os.getenv('MASTER_PATH', '/master')
        exhibitor_path = kwargs.get('master_path', None) or os.getenv('EXHIBITOR_PATH', '/')
        marathon_path = kwargs.get('marathon_path', None) or os.getenv('MARATHON_PATH', '/v2/apps')
        master_port = kwargs.get('master_port', None) or os.getenv('MASTER_PORT', 5050)
        exhibitor_port = kwargs.get('master_port', None) or os.getenv('EXHIBITOR_PORT', 8181)
        marathon_port = kwargs.get('marathon_port', None) or os.getenv('MARATHON_PORT', 8080)

        dcos_urls = [master_path, exhibitor_path, marathon_path]
        for url in dcos_urls:
            if not url.endswith('/'):
                url = url + '/'

        self.master_url = '{}:{}{}'.format(dcos_leader_url,master_path,master_port)
        self.exhibitor_url = '{}:{}{}'.format(dcos_leader_url, exhibitor_path, exhibitor_port)
        self.marathon_url = '{}:{}{}'.format(dcos_leader_url, marathon_path,marathon_port)
        self.zookeeper_path = kwargs.get('zookeeper_path', None) or os.getenv('ZOOKEEPER_PATH', True)
        self.verify_certs = kwargs.get('verify_certs', None) or os.getenv('VERIFY_CERTS', True)
        self.marathon_app_id = kwargs.get('marathon_app_id', None) or os.environ.get('MARATHON_APP_ID', None)
        self.username = kwargs.get('username', None) or os.environ.get('USERNAME', None)
        self.password = kwargs.get('password', None) or os.environ.get('PASSWORD', None)
        self.principal = kwargs.get('principal', None) or os.environ.get('PRINCIPAL', None)
        self.auth_token = kwargs.get('auth_token', None) or os.environ.get('AUTH_TOKEN', None)
        self.role = kwargs.get('role', None) or os.environ.get('ROLE', None)
        self.request_headers = None

        if self.password:
            os.environ.pop('PASSWORD')

        if not self.verify_certs:
            # Disable certificate verification
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

        if log_level == 'DEBUG':
            http_client.HTTPConnection.debuglevel = 1
            requests_log = logging.getLogger('requests.packages.urllib3')
            requests_log.setLevel(logging.DEBUG)
            requests_log.propagate = True

        # logging.INFO('Master: {} Exhibitor: {} Role: {} Principal: {} ZK Path: {}'.format(
            # self.master_url, self.exhibitor_url, self.role, self.principal, self.zookeeper_path))

    def request_url(self, method, url, data={}):
        logging.info('HTTP {}: {}'.format(method, url))
        try:
            return requests.request(method, url, headers=self.request_headers, data=data, json=json, verify=self.verify_certs)
        except requests.exceptions.Timeout:
            logging.error('HTTP {} request timed out.'.format(method))
        except requests.exceptions.ConnectionError as err:
            logging.error('Network error: {}'.format(err))
        except requests.exceptions.HTTPError as err:
            logging.error('Invalid HTTP response: {}'.format(err))
        return None


    def extract_version_num(self, slaves_json):
        '''"0.28.0" => 28, "1.2.0" => 102'''
        if not slaves_json:
            print('Bad slaves response')
            return None
        # check the version advertised by the slaves
        for slave in slaves_json.get('slaves', []):
            version = slave.get('version', None)
            if version:
                break
        if not version:
            logging.info('No version found in slaves list')
        version_parts = version.split('.')
        if len(version_parts) < 2:
            logging.error('Bad version string: {}'.format(version))
            return None
        version_val = 100 * int(version_parts[0]) + int(version_parts[1])
        logging.info('Mesos version: {} => {}'.format(version, version_val))


    def get_state_json(self):
        version_num = self.__master_versions.get(self.master_url, None)
        slaves_json = None
        if not version_num:
            # get version num from /slaves (and reuse response for volume info if version is >= 28)
            response = self.request_url('GET', urljoin(self.master_url, 'slaves'), self.request_headers)
            response.raise_for_status()
            version_num = self.__master_versions[self.master_url] = self.extract_version_num(response.json())
        if not version_num or version_num >= 28:
            # 0.28 and after only have the reservation data in /slaves
            if not slaves_json:
                response = self.request_url('GET', urljoin(self.master_url, 'slaves'), self.request_headers)
                response.raise_for_status()
                slaves_json = response.json()
            return slaves_json
        else:
            # 0.27 and before only have the reservation data in /state.json
            response = self.request_url('GET', urljoin(self.master_url, 'state.json'), self.request_headersreq_headers)
            response.raise_for_status()
            return response.json()


    def destroy_volumes(self):
        state = self.get_state_json(self.master_url, self.request_headers)
        if not state or not 'slaves' in state.keys():
            logging.error('Missing data in state response: {}'.format(state))
            return False
        all_success = True
        for slave in state['slaves']:
            if not self.destroy_volume(slave):
                all_success = False
        return all_success


    def destroy_volume(self, slave):
        volumes = []
        slave_id = slave['id']

        reserved_resources_full = slave.get('reserved_resources_full', None)
        if not reserved_resources_full:
            logging.info('No reserved resources for any role on slave {}'.format(slave_id))
            return True

        reserved_resources = reserved_resources_full.get(self.role, None)
        if not reserved_resources:
            logging.info('No reserved resources for role \'{}\' on slave {}. Known roles are: [{}]'.format(
                self.role,
                slave_id,
                ', '.join(reserved_resources_full.keys()))
            )
            return True

        for reserved_resource in reserved_resources:
            name = reserved_resource.get('name', None)
            disk = reserved_resource.get('disk', None)

            if name == 'disk' and disk and 'persistence' in disk:
                volumes.append(reserved_resource)

                logging.info('Found {} volume(s) for role \'{}\' on slave {}, deleting...'.format(
                    len(volumes), self.role, slave_id)
                )

        req_url = urljoin(self.master_url, 'destroy-volumes')
        data = {
            'slaveId': slave_id,
            'volumes': json.dumps(volumes)
        }
        logging.info('Found {} resource(s) for role \'{}\' on slave {}, deleting...'.format(
            len(volumes), self.role, slave_id)
        )

        response = self.request_url('POST', req_url, self.request_headers, data=data)
        logging.info('{} {}'.format(response.status_code, response.content))
        success = 200 <= response.status_code < 300
        if response.status_code == 409:
            logging.error('''###\nIs a framework using these resources still installed?\n###''')
        return success


    def delete_zk_node(self, znode):
        """Delete Zookeeper node via Exhibitor (eg http://leader.mesos:8181/exhibitor/v1/...)"""
        znode_url = urljoin(self.exhibitor_url, 'exhibitor/v1/explorer/znode/{}'.format(znode))

        response = self.request_url('DELETE', znode_url, self.request_headers)
        if not response:
            return False

        if 200 <= response.status_code < 300:
            logging.info('Successfully deleted znode \'{}\' (code={}), if znode existed.'.format(
                znode, response.status_code))
            return True
        else:
            logging.error('ERROR: HTTP DELETE request returned code:', response.status_code)
            logging.info('Response body:', response.text)
            return False


    def unreserve_resources(self):
        """Iterates all slave nodes for reservation removal"""
        state = self.get_state_json(self.master_url, self.request_headers)
        if not state or 'slaves' not in state.keys():
            return False
        all_success = True
        for slave in state['slaves']:
            if not self.unreserve_resource(slave):
                all_success = False
        return all_success


    def unreserve_resource(self, slave):
        """ removes all reservations from slave nodes 
        Args:
            slave: 
            
        Returns:

        """
        resources = []
        slave_id = slave['id']

        reserved_resources_full = slave.get('reserved_resources_full', None)
        if not reserved_resources_full:
            logging.info('No reserved resources for any role on slave {}'.format(slave_id))
            return True

        reserved_resources = reserved_resources_full.get(self.role, None)
        if not reserved_resources:
            logging.info('No reserved resources for role \'{}\' on slave {}. Known roles are: [{}]'.format(
                self.role, slave_id, ', '.join(reserved_resources_full.keys())))
            return True

        for reserved_resource in reserved_resources:
            resources.append(reserved_resource)

            logging.info('Found {} resource(s) for role \'{}\' on slave {}, deleting...'.format(
                len(resources), self.role, slave_id))

        req_url = urljoin(self.master_url, 'unreserve')
        data = {
            'slaveId': slave_id,
            'resources': json.dumps(resources)
        }
        logging.info('Request URL: {} || Request data: {}'.format(req_url, data))
        
        response = self.request_url('POST', req_url, self.request_headers, data=data)
        logging.info('{} {}'.format(response.status_code, response.content))
        return 200 <= response.status_code < 300


    def delete_self_if_marathon(self, marathon_url, req_headers={}):
        '''HACK: automatically delete ourselves from Marathon so that we don't execute in a loop'''

        if not self.marathon_app_id:
            return

        logging.info('Deleting self from Marathon to avoid run loop: {}'.format(self.marathon_app_id))
        marathon_app_url = urljoin(marathon_url, '{}'.format(self.marathon_app_id.strip('/')))
        response = self.request_url('DELETE', marathon_app_url, req_headers)
        if 200 <= response.status_code < 300:
            logging.info('Successfully deleted self from marathon (code={}): {}'.format(
                response.status_code, self.marathon_app_id))
        else:
            logging.error('ERROR: HTTP DELETE request returned code:', response.status_code)
            logging.error('Response body:', response.text)

    def auth(self):
        if not self.auth_token:
            if self.username and self.password:
                # fetch auth token using provided username/pw
                # http://leader.mesos:5050/some/long/path => http://leader.mesos:5050
                response = self.request_url(
                    'POST',
                    '{}://{}'.format(self.protocol, self.master_url),
                    json={'uid': self.username, 'password': self.password}
                )

                if 200 <= response.status_code < 300:
                    self.auth_token= response.json()['token']
                else:
                    logging.error(
                        'Authentication to Admin Router / Token retrieval failed: {} {}'.format(
                            response.status_code,
                            response.content
                        )
                    )

                    self.delete_self_if_marathon(self.marathon_url)  # fatal failure
                    sys.exit(4)
            else:
                logging.error(
                    'Unable to Authenticate to Admin Router - UserName/Password required'
                )
                sys.exit(4)
        self.request_headers = {'Authorization': 'token={}'.format(self.auth_token)}


    def clean(self):
        if self.role and self.principal or self.zookeeper_path:
            if self.role and self.principal:
                logging.info('Destroying volumes...')
                if not self.destroy_volumes():
                    logging.error('Deleting volumes failed, skipping other steps.')
                    return False  # resources will fail to delete if this fails
                logging.info('Unreserving resources...')
                if not self. unreserve_resources():
                    logging.error('Deleting resources failed, skipping other steps.')
                    return False  # don't delete ZK if this fails, that'll likely leave things in inconsistent state
            if self.zookeeper_path:
                logging.info('Deleting zk node...')
                if not self.delete_zk_node(self.zookeeper_path):
                    logging.error('Deleting zookeeper node failed.')
                    return False
            logging.info('Cleanup completed successfully.')
            return True
        else:   
            logging.error('Could not clean cluster: please provide role & principal | zookeeper_path')
            return False
        


    def main(self):
        if not self.clean():
            logging.error('Janitor Failed')
        self.delete_self_if_marathon() # success, don't try again!


if __name__ == '__main__':
    TheJanitor = DCOS_Janitor()
    TheJanitor.main()
