"""
Kanboard service

Pulls kanboard cards as tasks.

Kanboard API documentation available at https://docs.kanboard.org/en/latest/api/index.html
"""
from __future__ import unicode_literals
from future import standard_library
standard_library.install_aliases()
from six.moves.configparser import NoOptionError

from jinja2 import Template
from kanboard import Kanboard
import requests

from bugwarrior.services import IssueService, Issue, ServiceClient
from bugwarrior.config import die, asbool, aslist

DEFAULT_LABEL_TEMPLATE = "{{label|replace(' ', '_')}}"


class KanboardIssue(Issue):
    NAME = 'kanboardcard'
    CARDID = 'kanboardcardid'
    BOARD = 'kanboardboard'
    LIST = 'kanboardlist'
    SHORTLINK = 'kanboardshortlink'
    SHORTURL = 'kanboardshorturl'
    URL = 'kanboardurl'

    UDAS = {
        NAME: {'type': 'string', 'label': 'Kanboard card name'},
        CARDID: {'type': 'string', 'label': 'Kanboard card ID'},
        BOARD: {'type': 'string', 'label': 'Kanboard board name'},
        LIST: {'type': 'string', 'label': 'Kanboard list name'},
        SHORTLINK: {'type': 'string', 'label': 'Kanboard shortlink'},
        SHORTURL: {'type': 'string', 'label': 'Kanboard short URL'},
        URL: {'type': 'string', 'label': 'Kanboard URL'},
    }
    UNIQUE_KEY = (CARDID,)

    def get_default_description(self):
        """ Return the old-style verbose description from bugwarrior.
        """
        return self.build_default_description(
            title=self.record['name'],
            url=self.record['shortUrl'],
            number=self.record['idShort'],
            cls='task',
        )

    def get_tags(self, twdict):
        tmpl = Template(
            self.origin.get('label_template', DEFAULT_LABEL_TEMPLATE))
        return [tmpl.render(twdict, label=label['name'])
                for label in self.record['labels']]

    def to_taskwarrior(self):
        twdict = {
            'project': self.extra['boardname'],
            'priority': 'M',
            'due': self.parse_date(self.record['due']),
            self.NAME: self.record['name'],
            self.CARDID: self.record['id'],
            self.BOARD: self.extra['boardname'],
            self.LIST: self.extra['listname'],
            self.SHORTLINK: self.record['shortLink'],
            self.SHORTURL: self.record['shortUrl'],
            self.URL: self.record['url'],
            'annotations': self.extra.get('annotations', []),
        }
        if self.origin['import_labels_as_tags']:
            twdict['tags'] = self.get_tags(twdict)
        return twdict


class KanboardService(IssueService, ServiceClient):
    ISSUE_CLASS = KanboardIssue
    # What prefix should we use for this service's configuration values
    CONFIG_PREFIX = 'kanboard'
    KANBOARD_CONNECTION = None

    @classmethod
    def validate_config(cls, service_config, target):
        def check_key(opt):
            """ Check that the given key exist in the configuration  """
            if opt not in service_config:
                die("[{}] has no 'kanboard.{}'".format(target, opt))
        super(KanboardService, cls).validate_config(service_config, target)
        check_key('api_key')

    def get_service_metadata(self):
        """
        Return extra config options to be passed to the KanboardIssue class
        """
        return {
            'import_labels_as_tags':
            self.config.get('import_labels_as_tags', False, asbool),
            'label_template':
            self.config.get('label_template', DEFAULT_LABEL_TEMPLATE),
            }

    def issues(self):
        """
        Returns a list of dicts representing issues from a remote service.
        """
        for board in self.get_boards():
            for lst in self.get_lists(board['id']):
                listextra = dict(boardname=board['name'], listname=lst['name'])
                for card in self.get_cards(lst['id']):
                    for subtask in self.get_subtasks(card):
                        issue = self.get_issue_for_record(subtask, extra=listextra)
                        issue.update_extra({"annotations": self.annotations(subtask)})
                        yield issue

                    issue = self.get_issue_for_record(card, extra=listextra)
                    issue.update_extra({"annotations": self.annotations(card)})
                    yield issue

    def annotations(self, card_json):
        """ A wrapper around get_comments that build the taskwarrior
        annotations. """
        comments = self.get_comments(card_json['id'])
        annotations = self.build_annotations(
            ((c['memberCreator']['username'], c['data']['text']) for c in comments),
            card_json["shortUrl"])
        return annotations


    def get_boards(self):
        """
        Get the list of boards to pull cards from.  If the user gave a value to
        kanboard.include_boards use that, otherwise ask the Kanboard API for the
        user's boards.
        """
        self.connect()
        if 'include_boards' in self.config: # this should be board ids
            for boardid in self.config.get('include_boards', to_type=aslist):
                # Get the board name
                yield self.KANBOARD_CONNECTION.get_project_by_id(project_id=boardid)
        else:
            boards = self.KANBOARD_CONNECTION.get_all_projects()
            for board in boards:
                yield board

    def get_lists(self, board):
        """
        Returns a list of the filtered lists for the given board
        This filters the kanboard lists according to the configuration values of
        kanboard.include_lists and kanboard.exclude_lists.
        """
        self.connect()
        lists = self.KANBOARD_CONNECTION.get_columns(project_id=board).result

        include_lists = self.config.get('include_lists', to_type=aslist)
        if include_lists:
            lists = [l for l in lists if l['title'] in include_lists]

        exclude_lists = self.config.get('exclude_lists', to_type=aslist)
        if exclude_lists:
            lists = [l for l in lists if l['title'] not in exclude_lists]

        return lists

    def get_cards(self, list_id):
        """ Returns an iterator for the cards in a given list, filtered
        according to configuration values of kanboard.only_if_assigned and
        kanboard.also_unassigned """
        self.connect()

        cards = self.KANBOARD_CONNECTION.get_all_tasks(project_id=list_id, status_id=1).result # active tasks only

        for card in cards:
            yield card

    def get_comments(self, card_id):
        """ Returns an iterator for the comments on a certain card. """
        self.connect()

        comments = self.KANBOARD_CONNECTION.get_all_comments(task_id=card_id).result
        for comment in comments:
            yield comment

    def get_subtasks(self, card_id):
        """ Returns an iterator for subtasks on a certain card. """
        self.connect()

        subtasks = self.KANBOARD_CONNECTION.get_all_subtasks(task_id=card_id).result
        for subtask in subtasks:
            yield subtask


    def connect(self):
        """
        Make a kanboard API request. This takes an absolute url (without protocol
        and host) and a list of argumnets and return a GET request with the
        key and token from the configuration
        """

        if not self.config.get('base_url').startswith('http'):
            url = 'https://' + self.config.get('base_url')
        else:
                url = self.config.get('base_url')

        if not self.KANBOARD_CONNECTION:
            self.KANBOARD_CONNECTION = Kanboard(url, 'jsonrpc', self.config.get('token'))
