from airflow.utils.decorators import apply_defaults

from airflow.models import BaseOperator, Variable, SkipMixin
from airflow.hooks import S3Hook
from HubspotPlugin.hooks.hubspot_hook import HubspotHook

from os import path
from flatten_json import flatten
import logging
import json
import boa


class HubspotToS3Operator(BaseOperator, SkipMixin):
    """
    Hubspot To S3 Operator

    NOTE: A number of endpoints have nested arrays
    that are moved into their own table. In situations
    like this, the secondary table will have the prefix
    of the main Hubspot object.

    Example: The "Form Submissions" list of dictionaries
    in the contacts object will become it's own table
    with the label "contacts_form_submissions".

    :param hubspot_conn_id:          The Hubspot connection id.
    :type hubspot_conn_id:           string
    :param hubspot_object:           The desired Hubspot object. The currently
                                     supported values are:
                                        - campaigns
                                        - companies
                                        - contacts
                                        - contacts_by_company
                                        - deals
                                        - deal_pipelines
                                        - events
                                        - engagements
                                        - forms
                                        - keywords
                                        - lists
                                        - social
                                        - owners
                                        - timeline
                                        - workflows
    :type hubspot_object:            string
    :param payload:                  The associated hubspot parameters to
                                     pass into the object request as
                                     keyword arguments.
    :type payload:                   dict
    :param s3_conn_id:               The s3 connection id.
    :type s3_conn_id:                string
    :param s3_bucket:                The S3 bucket to be used to store
                                     the Hubspot data.
    :type s3_bucket:                 string
    :param s3_key:                   The S3 key to be used to store
                                     the Hubspot data.
    :type s3_key:                    string
    """

    template_fields = ('s3_key',)

    @apply_defaults
    def __init__(self,
                 hubspot_conn_id,
                 hubspot_object,
                 s3_conn_id,
                 s3_bucket,
                 s3_key,
                 payload={},
                 **kwargs):
        super().__init__(**kwargs)
        self.hubspot_conn_id = hubspot_conn_id
        self.hubspot_object = hubspot_object
        self.payload = payload
        self.s3_conn_id = s3_conn_id
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key

        if self.hubspot_object.lower() not in ('campaigns',
                                               'companies',
                                               'contacts',
                                               'contacts_by_company',
                                               'deals',
                                               'deal_pipelines',
                                               'events',
                                               'engagements',
                                               'forms',
                                               'keywords',
                                               'lists',
                                               'owners',
                                               'social',
                                               'timeline',
                                               'workflows',):
            raise Exception('{0} is not a currently supported queryable object.'
                            .format(self.hubspot_object))

    def execute(self, context):
        h = HubspotHook(self.hubspot_conn_id)
        self.split = path.splitext(self.s3_key)

        if self.hubspot_object.lower() == 'campaigns':
            campaigns = self.retrieve_data(h, context, "email/public/v1/campaigns")
            print("RAW CAMPAIGNS: " + str(campaigns))
            final_output = []
            for campaign in campaigns[0]['core']:
                print("CAMPAIGN ID: " + str(campaign['id']))
                output = self.retrieve_data(h, context, campaign_id=campaign['id'])
                output = output[0]['core']
                final_output.extend(output)
            self.outputManager(final_output, '{0}_core_final{1}'.format(self.split[0], self.split[1]), self.s3_bucket)
        elif self.hubspot_object.lower() == 'contacts_by_company':
            companies = self.retrieve_data(h, context, endpoint=self.methodMapper('companies'))
            print('Received companies list...')
            print(companies)
            if not companies:
                logging.info('No companies currently available.')
                downstream_tasks = context['task'].get_flat_relatives(upstream=False)
                logging.info('Skipping downstream tasks...')
                logging.debug("Downstream task_ids %s", downstream_tasks)
                if downstream_tasks:
                    self.skip(context['dag_run'], context['ti'].execution_date, downstream_tasks)
                return True
            final_output = []
            for company in companies:
                output = self.retrieve_data(h, context, company_id=company['companyId'])
                final_output.extend(output)
            self.outputManager(output, '{0}_core_final{1}'.format(self.split[0], self.split[1]), self.s3_bucket)
        else:
            output = self.retrieve_data(h, context)

            for e in output:
                for k, v in e.items():
                    if k == 'core':
                        key = '{0}_core_final{1}'.format(self.split[0], self.split[1])
                    else:
                        key = '{0}_{1}_final{2}'.format(self.split[0],
                                                         boa.constrict(k),
                                                         self.split[1])
                    self.outputManager(v, key, self.s3_bucket)

    def outputManager(self, output, key, bucket):
        logging.info('Logging {0} to S3...'.format(key))
        output = [flatten(e) for e in output]
        output = '\n'.join([json.dumps({boa.constrict(k): v
                           for k, v in i.items()}) for i in output])
        s3 = S3Hook(self.s3_conn_id)
        s3.load_string(
            string_data=str(output),
            key=key,
            bucket_name=bucket,
            replace=True
        )
        s3.connection.close()

    def retrieve_data(self,
                      h,
                      context,
                      endpoint=None,
                      company_id=None,
                      campaign_id=None):
        if endpoint is None:
            print('ENDPOINT IS NONE.')
            endpoint = self.methodMapper(self.hubspot_object,
                                         company_id=company_id,
                                         campaign_id=campaign_id)
            print('ENDPOINT IS NOW: ' + str(endpoint))

        return self.paginate_data(h,
                                  endpoint,
                                  context,
                                  company_id=company_id,
                                  campaign_id=campaign_id)

    def paginate_data(self,
                      h,
                      endpoint,
                      context,
                      company_id=None,
                      campaign_id=None):
        """
        This method takes care of request building and pagination.
        It retrieves 100 at a time and continues to make
        subsequent requests until it retrieves less than 100 records.
        """
        output = []
        final_payload = {'count': 100,
                         'vidOffset': 0}

        for param in self.payload:
            final_payload[param] = self.payload[param]
        response = h.run(endpoint, final_payload).json()
        if not response:
            logging.info('Resource Unavailable.')
            return ''
        if self.hubspot_object == 'owners':
            output.extend([e for e in response])
            output = [self.filterMapper(record) for record in output]
            output = self.subTableMapper(output)
            return output
        elif self.hubspot_object == 'engagements':
            output.extend([e for e in response['results']])
        elif self.hubspot_object == 'contacts_by_company':
            if endpoint == 'companies/v2/companies/paged':
                if response['companies']:
                    output.extend([e for e in response['companies']])
                else:
                    logging.info('No companies currently available.')
                    return ''
            else:
                output.extend([{"vid": e, "company_id": company_id}
                               for e in response['vids']])
        elif self.hubspot_object == 'campaigns':
            if 'email/public/v1/campaigns/' in endpoint:
                output.append(response)
        elif self.hubspot_object in ('deal_pipelines', 'social'):
                output.extend([e for e in response])
        else:
            output.extend([e for e in response[self.hubspot_object]])

        if isinstance(response, dict):
            if 'hasMore' in list(response.keys()):
                more = 'hasMore'
            elif 'has-more' in list(response.keys()):
                more = 'has-more'
            else:
                more = 'has-more'
                response['has-more'] = False
            n = 0
            while response[more] is True:
                if 'vid-offset' in list(response.keys()):
                    offset_variable = 'vid-offset'
                    final_payload['vidOffset'] = response['vid-offset']
                    logging.info('Retrieving: ' + str(response['vid-offset']))
                elif 'offset' in list(response.keys()):
                    offset_variable = 'offset'
                    final_payload['offset'] = response['offset']
                    logging.info('Retrieving: ' + str(response['offset']))
                try:
                    response = h.run(endpoint, final_payload).json()
                except:
                    logging.debug('Request was unsuccessful. Trying again.')
                    pass

                output.extend([e for e in response[self.hubspot_object]])

                n += 1
                # time.sleep(0.2)
                if n % 100 == 0:
                    output = [self.filterMapper(record) for record in output]
                    output = self.subTableMapper(output)
                    if self.hubspot_object.lower() == 'contacts_by_company':
                        companies = self.retrieve_data(h, self.methodMapper('companies'))
                        if not companies:
                            logging.info('No companies currently available.')
                            downstream_tasks = context['task'].get_flat_relatives(upstream=False)
                            logging.info('Skipping downstream tasks...')
                            logging.debug("Downstream task_ids %s", downstream_tasks)
                            if downstream_tasks:
                                self.skip(context['dag_run'], context['ti'].execution_date, downstream_tasks)
                            return True
                        final_output = []
                        for company in companies:
                            final_output.extend(output)
                        key = '{0}_core_{1}{2}'.format(self.split[0],
                                                  str(n),
                                                  self.split[1])
                        self.outputManager(output, key, self.s3_bucket)
                    else:
                        for e in output:
                            for k, v in e.items():
                                if k == 'core':
                                    key = '{0}_core_{1}{2}'.format(self.split[0],
                                                                   str(n),
                                                                   self.split[1])
                                else:
                                    key = '{0}_{1}_{2}{3}'.format(self.split[0],
                                                                  boa.constrict(k),
                                                                  str(n),
                                                                  self.split[1])
                                logging.info('Sending to Output Manager...')
                                self.outputManager(v, key, self.s3_bucket)

                    (Variable.set('{0}_vidOffset'
                                  .format(context['ti']
                                          .get_template_context()
                                          ['task_instance_key_str']),
                                  response[offset_variable]))
                    output = []

        output = [self.filterMapper(record) for record in output]
        output = self.subTableMapper(output)
        return output

    def methodMapper(self, hubspot_object, company_id=None, campaign_id=None):
        """
        This method maps the desired object to the relevant endpoint
        according to v3 of the Hubspot API.
        """
        mapping = {"campaigns": "email/public/v1/campaigns/{0}"
                                .format(campaign_id),
                   "companies": "companies/v2/companies/paged",
                   "contacts": "contacts/v1/lists/all/contacts/all",
                   "contacts_by_company": "companies/v2/companies/{0}/vids"
                                          .format(company_id),
                   "deals": "deals/v1/deal/paged",
                   "deal_pipelines": "/deals/v1/pipelines",
                   "events": "email/public/v1/events",
                   "engagements": "engagements/v1/engagements/paged",
                   "forms": "forms/v2/forms",
                   "keywords": "keywords/v1/keywords",
                   "lists": "contacts/v1/lists",
                   "social": "broadcast/v1/channels/setting/publish/current",
                   "owners": "owners/v2/owners",
                   "timeline": "email/public/v1/subscriptions/timeline",
                   "workflows": "automation/v3/workflows"
                   }

        return mapping[hubspot_object]

    def subTableMapper(self, output):
        """
        This mapper expects a list of either dictionaries
        or string values as specified in the 'split' value
        of the mapping and then outputs them to a new object.
        """
        mapping = [{'name': 'contacts',
                    'split': 'form-submissions',
                    'retained': [{'vid': 'vid'}]
                    },
                   {'name': 'contacts',
                    'split': 'identity-profiles.identities',
                    'retained': [{"addedAt": "addedAt"}]
                    },
                   {'name': 'contacts',
                    'split': 'merge-audits',
                    'retained': [{'vid': 'vid'}]
                    },
                   {'name': 'contacts',
                    'split': 'merged-vids',
                    'retained': [{"vid": "vid"}]
                    },
                   {'name': 'contacts',
                    'split': 'list-memberships',
                    'retained': []
                    },
                   {'name': 'deals',
                    'split': 'associations.associatedVids',
                    'retained': [{"dealId": "deal_id"}]
                    },
                   {'name': 'deals',
                    'split': 'associations.associatedCompanyIds',
                    'retained': [{"dealId": "deal_id"}]
                    },
                   {'name': 'deals',
                    'split': 'associations.associatedDealIds',
                    'retained': [{"dealId": "deal_id"}]
                    },
                   {'name': 'deal_pipelines',
                    'split': 'stages',
                    'retained': [{"pipelineId": "pipeline_id"}]
                    },
                   {'name': 'forms',
                    'split': 'formFieldGroups',
                    'retained': [{'guid': 'form_id'}]
                    },
                   {'name': 'lists',
                    'split': 'filters',
                    'retained': []
                    },
                   {'name': 'owners',
                    'split': 'remoteList',
                    'retained': []
                    },
                   {'name': 'timeline',
                    'split': 'changes',
                    'retained': [{'timestamp': 'timestamp'},
                                 {'recipient': 'recipient'}]
                    },
                   {'name': 'workflows',
                    'split': 'personaTagIds',
                    'retained': [{'id': 'workflow_id'}]
                    },
                   {'name': 'workflows',
                    'split': 'contactListIds',
                    'retained': [{'id': 'workflow_id'}]
                    }]

        def process_record(record, mapping):
            final_returnable_dict = {}
            for entry in mapping:
                returnable_list = []
                if ((entry['name'] == self.hubspot_object)
                   and (entry['split'] in list(record.keys()))):
                    for item in record[entry['split']]:
                        returnable_dict = {}
                        if isinstance(item, dict):
                            returnable_dict = item
                        elif isinstance(item, str):
                            (returnable_dict['{0}'.format(entry['split'])]
                             == record[entry['split']])
                        for item in entry['retained']:
                            for k, v in item.items():
                                returnable_dict[v] = record[k]
                        returnable_list.append(returnable_dict)
                    del record[entry['split']]
                if returnable_list:
                    final_returnable_dict[entry['split']] = returnable_list

                final_returnable_dict['core'] = record
            return final_returnable_dict

        def process_data(output):
            output = [process_record(record, mapping) for record in output]
            final_output = []
            output2 = {}
            output2['core'] = [e.pop('core') for e in output]
            final_output.append(output2)
            for entry in mapping:
                output2 = {}
                if (entry['name'] == self.hubspot_object):
                    output2[entry['split']] = [e.pop(entry['split']) for e in output
                                               if (entry['split'] in list(e.keys()))]
                    output2[entry['split']] = [item for sublist in output2[entry['split']]
                                               for item in sublist]
                    if not output2[entry['split']]:
                        del output2[entry['split']]
                    final_output.append(output2)
            final_output = [e for e in final_output if e]
            return final_output

        return process_data(output)

    def filterMapper(self, record):
        """
        This process strips out unnecessary objects (i.e. ones
        that are duplicated in other core objects).
        """
        mapping = [{'name': 'commits',
                    'filtered': 'author',
                    'retained': ['id']
                    }]

        def process(record, mapping):
            """
            This method processes the data according to the above mapping.
            There are a number of checks throughout as the specified filtered
            object and desired retained fields will not always exist in each
            record.
            """

            for entry in mapping:
                # Check to see if the filtered value exists in the record
                if (entry['name'] == self.hubspot_object)\
                 and (entry['filtered'] in list(record.keys())):
                    # Check to see if any retained fields are desired.
                    # If not, delete the object.
                    if entry['retained']:
                        for retained_item in entry['retained']:
                            # Check to see the filterable object exists in the
                            # specific record. This is not always the case.
                            # Check to see the retained field exists in the
                            # filterable object.
                            if record[entry['filtered']] is not None\
                             and retained_item in list(record[entry['filtered']].keys()):
                                # Bring retained field to top level of
                                # object with snakecasing.
                                record["{0}_{1}".format(entry['filtered'],
                                                        retained_item)] = \
                                    record[entry['filtered']][retained_item]
                    if record[entry['filtered']] is not None:
                        del record[entry['filtered']]
            return record

        return process(record, mapping)