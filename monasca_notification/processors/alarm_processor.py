# Copyright (c) 2014 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import multiprocessing
import MySQLdb
import statsd
import time

from monasca_notification.notification import Notification
from monasca_notification.notification_exceptions import AlarmFormatError
from monasca_notification.processors.base import BaseProcessor


log = logging.getLogger(__name__)


class AlarmProcessor(BaseProcessor):
    def __init__(
            self, alarm_queue, notification_queue, finished_queue,
            alarm_ttl, mysql_host, mysql_user, mysql_passwd, dbname):
        self.alarm_queue = alarm_queue
        self.alarm_ttl = alarm_ttl
        self.notification_queue = notification_queue
        self.finished_queue = finished_queue

        try:
            self.mysql = MySQLdb.connect(host=mysql_host, user=mysql_user, passwd=mysql_passwd, db=dbname)
            self.mysql.autocommit(True)
        except:
            log.exception('MySQL connect failed')
            raise

    @staticmethod
    def _parse_alarm(alarm_data):
        """Parse the alarm message making sure it matches the expected format.
        """
        expected_fields = [
            'actionsEnabled',
            'alarmId',
            'alarmName',
            'newState',
            'oldState',
            'stateChangeReason',
            'tenantId',
            'timestamp'
        ]

        json_alarm = json.loads(alarm_data)
        alarm = json_alarm['alarm-transitioned']
        for field in expected_fields:
            if field not in alarm:
                raise AlarmFormatError('Alarm data missing field %s' % field)
        if ('tenantId' not in alarm) or ('alarmId' not in alarm):
            raise AlarmFormatError

        return alarm

    def _alarm_is_valid(self, alarm):
        """Check if the alarm is enabled and is within the ttl, return True in that case
        """
        if not alarm['actionsEnabled']:
            log.debug('Actions are disabled for this alarm.')
            return False

        alarm_age = time.time() - alarm['timestamp']  # Should all be in seconds since epoch
        if (self.alarm_ttl is not None) and (alarm_age > self.alarm_ttl):
            log.warn('Received alarm older than the ttl, skipping. Alarm from %s' % time.ctime(alarm['timestamp']))
            return False

        return True

    def run(self):
        """Check the notification setting for this project in mysql then create the appropriate notification or
             add to the finished_queue
        """
        cur = self.mysql.cursor()
        pname = multiprocessing.current_process().name
        failed_parse_count = statsd.Counter('AlarmsFailedParse-%s' % pname)
        no_notification_count = statsd.Counter('AlarmsNoNotification-%s' % pname)
        notification_count = statsd.Counter('NotificationsCreated-%s' % pname)
        db_time = statsd.Timer('ConfigDBTime-%s' % pname)

        while True:
            raw_alarm = self.alarm_queue.get()
            partition = raw_alarm[0]
            offset = raw_alarm[1].offset
            try:
                alarm = self._parse_alarm(raw_alarm[1].message.value)
            except Exception as e:  # This is general because of a lack of json exception base class
                failed_parse_count += 1
                log.error("Invalid Alarm format skipping partition %d, offset %d\nErrror%s" % (partition, offset, e))
                self._add_to_queue(self.finished_queue, 'finished', (partition, offset))
                continue

            log.debug("Read alarm from alarms sent_queue. Partition %d, Offset %d, alarm data %s"
                      % (partition, offset, alarm))

            if not self._alarm_is_valid(alarm):
                no_notification_count += 1
                self._add_to_queue(self.finished_queue, 'finished', (partition, offset))
                continue

            try:
                with db_time.time():
                    cur.execute("""SELECT name, type, address
                                   FROM alarm_action as aa
                                   JOIN notification_method as nm ON aa.action_id = nm.id
                                   WHERE aa.alarm_id = %s and aa.alarm_state = %s""",
                                [alarm['alarmId'], alarm['newState']])
            except MySQLdb.Error:
                log.exception('Mysql Error')
                raise

            notifications = [
                Notification(row[1].lower(), partition, offset, row[0], row[2], alarm) for row in cur]

            if len(notifications) == 0:
                no_notification_count += 1
                log.debug('No notifications found for this alarm, partition %d, offset %d, alarm data %s'
                          % (partition, offset, alarm))
                self._add_to_queue(self.finished_queue, 'finished', (partition, offset))
            else:
                notification_count += len(notifications)
                self._add_to_queue(self.notification_queue, 'notifications', notifications)
