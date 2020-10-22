# SPDX-License-Identifier: Apache-2.0

# Copyright 2020 Contributors to OpenLEADR

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
OpenADR Client for Python
"""

import asyncio
from functools import partial
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from random import randint
import logging

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openleadr import enums, objects
from openleadr.messaging import create_message, parse_message
from openleadr.utils import peek, generate_id, certificate_fingerprint, find_by, cron_config
import xmltodict

logger = logging.getLogger('openleadr')


class OpenADRClient:
    """
    Main client class. Most of these methods will be called automatically, but
    you can always choose to call them manually.
    """
    def __init__(self, ven_name, vtn_url, debug=False, cert=None, key=None,
                 passphrase=None, vtn_fingerprint=None, show_fingerprint=True):
        """
        Initializes a new OpenADR Client (Virtual End Node)

        :param str ven_name: The name for this VEN
        :param str vtn_url: The URL of the VTN (Server) to connect to
        :param bool debug: Whether or not to print debugging messages
        :param str cert: The path to a PEM-formatted Certificate file to use
                         for signing messages.
        :param str key: The path to a PEM-formatted Private Key file to use
                        for signing messages.
        :param str fingerprint: The fingerprint for the VTN's certificate to
                                verify incomnig messages
        :param str show_fingerprint: Whether to print your own fingerprint
                                     on startup. Defaults to True.
        """

        self.ven_name = ven_name
        self.vtn_url = vtn_url
        self.ven_id = None
        self.poll_frequency = None
        self.debug = debug
        self.reports = []
        self.report_requests = []               # Keep track of the report requests from the VTN
        self.pending_reports = asyncio.Queue()  # Holds reports that are waiting to be sent
        self.scheduler = AsyncIOScheduler()
        self.client_session = aiohttp.ClientSession()
        self.report_queue_task = None

        if cert and key:
            with open(cert, 'rb') as file:
                cert = file.read()
            with open(key, 'rb') as file:
                key = file.read()

            if show_fingerprint:
                print("")
                print("*" * 80)
                print("Your VEN Certificate Fingerprint is "
                      f"{certificate_fingerprint(cert).center(80)}")
                print("Please deliver this fingerprint to the VTN.".center(80))
                print("You do not need to keep this a secret.".center(80))
                print("*" * 80)
                print("")

        self._create_message = partial(create_message,
                                       cert=cert,
                                       key=key,
                                       passphrase=passphrase)
        self._parse_message = partial(parse_message,
                                      fingerprint=vtn_fingerprint)

    async def run(self):
        """
        Run the client in full-auto mode.
        """
        # if not hasattr(self, 'on_event'):
        #     raise NotImplementedError("You must implement on_event.")

        await self.create_party_registration()

        if not self.ven_id:
            logger.error("No VEN ID received from the VTN, aborting.")
            return

        if self.reports:
            await self.register_reports(self.reports)
            loop = asyncio.get_event_loop()
            self.report_queue_task = loop.create_task(self._report_queue_worker())

        await self._poll()

        # Set up automatic polling
        if self.poll_frequency.total_seconds() < 60:
            seconds_offset = randint(0, self.poll_frequency.seconds)
            cron_second = f"{seconds_offset}/{self.poll_frequency.seconds}"
            cron_minute = "*"
            cron_hour = "*"
        elif self.poll_frequency.total_seconds() < 3600:
            cron_second = randint(0, 59)
            cron_minute = f'*/{int(self.poll_frequency.total_seconds() / 60)}'
            cron_hour = "*"
        elif self.poll_frequency.total_seconds() < 86400:
            cron_second = randint(0, 59)
            cron_minute = "0"
            cron_hour = f'*/{int(self.poll_frequency.total_seconds() / 3600)}'
        elif self.poll_frequency.total_seconds() > 86400:
            logger.warning("Polling with intervals of more than 24 hours is not supported. "
                           "Will use 24 hours as the logging interval.")
            cron_second = randint(0, 59)
            cron_minute = "0"
            cron_hour = "0"
            return

        self.scheduler.add_job(self._poll,
                               trigger='cron',
                               second=cron_second,
                               minute=cron_minute,
                               hour=cron_hour)
        self.scheduler.start()

    async def stop(self):
        """
        Cleanly stops the client. Run this coroutine before closing your event loop.
        """
        if self.scheduler.running:
            self.scheduler.shutdown()
        if self.report_queue_task:
            self.report_queue_task.cancel()
        await self.client_session.close()

    def add_report(self, callable, resource_id, measurement,
                   report_specifier_id=None, r_id=None,
                   report_name=enums.REPORT_NAME.TELEMETRY_USAGE,
                   reading_type=enums.READING_TYPE.DIRECT_READ,
                   report_type=enums.REPORT_TYPE.READING, sampling_rate=None, data_source=None,
                   scale="none", unit=None, power_ac=True, power_hertz=50, power_voltage=230,
                   market_context=None):
        """
        Add a new reporting capability to the client.

        :param callable callable: A callable or coroutine that will fetch the value for a specific
                                  report. This callable will be passed the report_id and the r_id
                                  of the requested value.
        :param str resource_id: A specific name for this resource within this report.
        :param str measurement: The quantity that is being measured (openleadr.enums.MEASUREMENTS).
        :param str report_specifier_id: A unique identifier for this report. Leave this blank for a
                                        random generated id, or fill it in if your VTN depends on
                                        this being a known value, or if it needs to be constant
                                        between restarts of the client.
        :param str r_id: A unique identifier for a datapoint in a report. The same remarks apply as
                         for the report_specifier_id.
        :param str report_id: A unique identifier for this report.
        :param str report_name: An OpenADR name for this report (one of openleadr.enums.REPORT_NAME)
        :param str reading_type: An OpenADR reading type (found in openleadr.enums.READING_TYPE)
        :param str report_type: An OpenADR report type (found in openleadr.enums.REPORT_TYPE)
        :param datetime.timedelta sampling_rate: The sampling rate for the measurement.
        :param str unit: The unit for this measurement.

        """

        # Verify input
        if report_name not in enums.REPORT_NAME.values and not report_name.startswith('x-'):
            raise ValueError(f"{report_name} is not a valid report_name. Valid options are "
                             f"{', '.join(enums.REPORT_NAME.values)}",
                             " or any name starting with 'x-'.")
        if reading_type not in enums.READING_TYPE.values and not reading_type.startswith('x-'):
            raise ValueError(f"{reading_type} is not a valid reading_type. Valid options are "
                             f"{', '.join(enums.READING_TYPE.values)}"
                             " or any name starting with 'x-'.")
        if report_type not in enums.REPORT_TYPE.values and not report_type.startswith('x-'):
            raise ValueError(f"{report_type} is not a valid report_type. Valid options are "
                             f"{', '.join(enums.REPORT_TYPE.values)}"
                             " or any name starting with 'x-'.")
        if scale not in enums.SI_SCALE_CODE.values:
            raise ValueError(f"{scale} is not a valid scale. Valid options are "
                             f"{', '.join(enums.SI_SCALE_CODE.values)}")

        if sampling_rate is None:
            sampling_rate = objects.SamplingRate(min_period=timedelta(seconds=10),
                                                 max_period=timedelta(hours=24),
                                                 on_change=False)
        elif isinstance(sampling_rate, timedelta):
            sampling_rate = objects.SamplingRate(min_period=sampling_rate,
                                                 max_period=sampling_rate,
                                                 on_change=False)

        # Determine the correct item name, item description and unit
        if isinstance(measurement, objects.Measurement):
            item_base = measurement
        elif measurement.upper() in enums.MEASUREMENTS.members:
            item_base = enums.MEASUREMENTS[measurement.upper()]
        elif isinstance(measurement, str):
            item_base = objects.Measurement(item_name='customUnit',
                                            item_description=measurement,
                                            item_units=unit)
        else:
            raise ValueError("measurement should be one of the MEASUREMENTS from enums, or a str")

        # Check if unit is compatible
        if unit is not None and unit != item_base.item_units \
                and unit not in item_base.acceptable_units:
            logger.warning(f"The supplied unit {unit} for measurement {measurement} "
                           f"will be ignored, {item_base['item_units']} will be used instead."
                           f"Allowed units for this measurement are: "
                           f"{', '.join(item_base.acceptable_units)}")
        item_base.si_scale_code = scale

        # Get or create the relevant Report
        if report_specifier_id:
            report = find_by(self.reports,
                             'report_name', report_name,
                             'report_specifier_id', report_specifier_id)
        else:
            report = find_by(self.reports, 'report_name', report_name)

        if not report:
            report_specifier_id = report_specifier_id or generate_id()
            report = objects.Report(created_date_time=datetime.now(),
                                    report_name=report_name,
                                    report_specifier_id=report_specifier_id)
            self.reports.append(report)

        # Add the new report description to the report
        target = objects.Target(resource_id=resource_id)
        report_description = objects.ReportDescription(r_id=generate_id(),
                                                       reading_type=reading_type,
                                                       report_data_source=target,
                                                       report_subject=target,
                                                       report_type=report_type,
                                                       sampling_rate=sampling_rate,
                                                       measurement=item_base,
                                                       market_context='Market01',
                                                       callable=callable)
        report.report_descriptions.append(report_description)

    ###########################################################################
    #                                                                         #
    #                             POLLING METHODS                             #
    #                                                                         #
    ###########################################################################

    async def poll(self):
        """
        Request the next available message from the Server. This coroutine is called automatically.
        """
        service = 'OadrPoll'
        message = self._create_message('oadrPoll', ven_id=self.ven_id)
        response_type, response_payload = await self._perform_request(service, message)
        return response_type, response_payload

    ###########################################################################
    #                                                                         #
    #                         REGISTRATION METHODS                            #
    #                                                                         #
    ###########################################################################

    async def query_registration(self):
        """
        Request information about the VTN.
        """
        request_id = generate_id()
        service = 'EiRegisterParty'
        message = self._create_message('oadrQueryRegistration', request_id=request_id)
        response_type, response_payload = await self._perform_request(service, message)
        return response_type, response_payload

    async def create_party_registration(self, http_pull_model=True, xml_signature=False,
                                        report_only=False, profile_name='2.0b',
                                        transport_name='simpleHttp', transport_address=None,
                                        ven_id=None):
        """
        Take the neccessary steps to register this client with the server.

        :param bool http_pull_model: Whether to use the 'pull' model for HTTP.
        :param bool xml_signature: Whether to sign each XML message.
        :param bool report_only: Whether or not this is a reporting-only client
                                 which does not deal with Events.
        :param str profile_name: Which OpenADR profile to use.
        :param str transport_name: The transport name to use. Either 'simpleHttp' or 'xmpp'.
        :param str transport_address: Which public-facing address the server should use
                                      to communicate.
        :param str ven_id: The ID for this VEN. If you leave this blank,
                           a VEN_ID will be assigned by the VTN.
        """
        request_id = generate_id()
        service = 'EiRegisterParty'
        payload = {'ven_name': self.ven_name,
                   'http_pull_model': http_pull_model,
                   'xml_signature': xml_signature,
                   'report_only': report_only,
                   'profile_name': profile_name,
                   'transport_name': transport_name,
                   'transport_address': transport_address}
        if ven_id:
            payload['ven_id'] = ven_id
        message = self._create_message('oadrCreatePartyRegistration',
                                       request_id=generate_id(),
                                       **payload)
        response_type, response_payload = await self._perform_request(service, message)
        if response_type is None:
            return
        if response_payload['response']['response_code'] != 200:
            status_code = response_payload['response']['response_code']
            status_description = response_payload['response']['response_description']
            logger.error(f"Got error on Create Party Registration: "
                         f"{status_code} {status_description}")
            return
        self.ven_id = response_payload['ven_id']
        self.poll_frequency = response_payload.get('requested_oadr_poll_freq',
                                                   timedelta(seconds=10))
        logger.info(f"VEN is now registered with ID {self.ven_id}")
        logger.info(f"The polling frequency is {self.poll_frequency}")
        return response_type, response_payload

    async def cancel_party_registration(self):
        raise NotImplementedError("Cancel Registration is not yet implemented")

    ###########################################################################
    #                                                                         #
    #                              EVENT METHODS                              #
    #                                                                         #
    ###########################################################################

    async def request_event(self, reply_limit=1):
        """
        Request the next Event from the VTN, if it has any.
        """
        payload = {'request_id': generate_id(),
                   'ven_id': self.ven_id,
                   'reply_limit': reply_limit}
        message = self._create_message('oadrRequestEvent', **payload)
        service = 'EiEvent'
        response_type, response_payload = await self._perform_request(service, message)
        return response_type, response_payload

    async def created_event(self, request_id, event_id, opt_type, modification_number=1):
        """
        Inform the VTN that we created an event.
        """
        service = 'EiEvent'
        payload = {'ven_id': self.ven_id,
                   'response': {'response_code': 200,
                                'response_description': 'OK',
                                'request_id': request_id},
                   'event_responses': [{'response_code': 200,
                                        'response_description': 'OK',
                                        'request_id': request_id,
                                        'event_id': event_id,
                                        'modification_number': modification_number,
                                        'opt_type': opt_type}]}
        message = self._create_message('oadrCreatedEvent', **payload)
        response_type, response_payload = await self._perform_request(service, message)

    ###########################################################################
    #                                                                         #
    #                             REPORTING METHODS                           #
    #                                                                         #
    ###########################################################################

    async def register_reports(self, reports):
        """
        Tell the VTN about our reports. The VTN miht respond with an
        oadrCreateReport message that tells us which reports are to be sent.
        """
        request_id = generate_id()
        payload = {'request_id': generate_id(),
                   'ven_id': self.ven_id,
                   'reports': reports}

        service = 'EiReport'
        message = self._create_message('oadrRegisterReport', **payload)
        response_type, response_payload = await self._perform_request(service, message)

        # Handle the subscriptions that the VTN is interested in.
        if 'report_requests' in response_payload:
            for report_request in response_payload['report_requests']:
                result = await self.create_report(report_request)

        message_type = 'oadrCreatedReport'
        message_payload = {}

        return response_type, response_payload

    async def create_report(self, report_request):
        """
        Add the requested reports to the reporting mechanism.
        This is called when the VTN requests reports from us.

        :param report_request dict: The oadrReportRequest dict from the VTN.
        """
        # Get the relevant variables from the report requests
        report_request_id = report_request['report_request_id']
        report_specifier_id = report_request['report_specifier']['report_specifier_id']
        report_back_duration = report_request['report_specifier'].get('report_back_duration')
        granularity = report_request['report_specifier']['granularity']
        if 'report_interval' in report_request['report_specifier']:
            report_interval = report_request['report_specifier']['report_interval']
        else:
            report_interval = None

        # Check if this report actually exists
        report = find_by(self.reports, 'report_specifier_id', report_specifier_id)
        if not report:
            logger.error(f"A non-existant report with report_specifier_id "
                         f"{report_specifier_id} was requested.")
            return False

        # Check and collect the requested r_ids for this report
        requested_r_ids = []
        for specifier_payload in report_request['report_specifier']['specifier_payloads']:
            r_id = specifier_payload['r_id']
            reading_type = specifier_payload['reading_type']

            # Look up this report in our own reports index to make sure it is valid

            # Check if the requested r_id actually exists
            rd = find_by(report.report_descriptions, 'r_id', r_id)
            if not rd:
                logger.error(f"A non-existant report with r_id {r_id} "
                             f"inside report with report_specifier_id {report_specifier_id} "
                             f"was requested.")
                continue

            # Check if the requested measurement exists and if the correct unit is requested
            if 'measurement' in specifier_payload:
                measurement = specifier_payload['measurement']
                if measurement['item_description'] != rd.measurement.item_description:
                    logger.error(f"A non-matching measurement description for report with "
                                 f"report_request_id {report_request_id} and r_id {r_id} was given "
                                 f"by the VTN. Offered: {rd.measurement.item_description}, "
                                 f"requested: {measurement['item_description']}")
                    continue
                if measurement['item_units'] != rd.measurement.item_units:
                    logger.error(f"A non-matching measurement unit for report with "
                                 f"report_request_id {report_request_id} and r_id {r_id} was given "
                                 f"by the VTN. Offered: {rd.measurement.item_units}, "
                                 f"requested: {measurement['item_units']}")
                    continue

            if granularity is not None:
                if not rd.sampling_rate.min_period <= granularity <= rd.sampling_rate.max_period:
                    logger.error(f"An invalid sampling rate {granularity} was requested for report "
                                 f"with report_specifier_id {report_specifier_id} and r_id {r_id}. "
                                 f"The offered sampling rate was between "
                                 f"{rd.sampling_rate.min_period} and "
                                 f"{rd.sampling_rate.max_period}")
                    continue
            else:
                # If no granularity is specified, set it to the lowest sampling rate.
                granularity = rd.sampling_rate.max_period

            # Parse the report interval to set limits on the data collection
            if 'report_interval' in report_request['report_specifier']:
                report_interval = report_request['report_specifier']
                report_start = report_interval['dtstart']
                report_duration = report_interval['duration']
            else:
                report_start = datetime.now(timezone.utc)
                duration = None

            requested_r_ids.append(r_id)

        callable = partial(self.update_report, report_request_id=report_request_id)
        job = self.scheduler.add_job(func=callable,
                                     trigger='cron',
                                     **cron_config(granularity))
        self.report_requests.append({'report_request_id': report_request_id,
                                     'report_specifier_id': report_specifier_id,
                                     'r_ids': requested_r_ids,
                                     'granularity': granularity,
                                     'job': job})

    async def update_report(self, report_request_id):
        """
        Call the previously registered report callable and send the result as a message to the VTN.
        """
        report_request = find_by(self.report_requests, 'report_request_id', report_request_id)
        report = find_by(self.reports, 'report_specifier_id', report_request['report_specifier_id'])
        intervals = []
        for r_id in report_request['r_ids']:
            specific_report = find_by(report.report_descriptions, 'r_id', r_id)
            result = specific_report.callable()
            if asyncio.iscoroutine(result):
                result = await result
            report_payload = objects.ReportPayload(r_id=r_id, value=result)
            intervals.append(objects.ReportInterval(dtstart=datetime.now(timezone.utc),
                                                    report_payload=report_payload))

        # breakpoint()
        report = objects.Report(report_request_id=report_request_id,
                                report_specifier_id=report.report_specifier_id,
                                report_name=report.report_name,
                                intervals=intervals)
        await self.pending_reports.put(report)

    async def cancel_report(self, payload):
        """
        Cancel this report.
        """

    async def _report_queue_worker(self):
        """
        A Queue worker that pushes out the pending reports.
        """

        while True:
            report = await self.pending_reports.get()

            service = 'EiReport'
            message = self._create_message('oadrUpdateReport', reports=[report])

            try:
                response_type, response_payload = await self._perform_request(service, message)
            except Exception as err:
                breakpoint()

            if 'cancel_report' in response_payload:
                await self.cancel_report(response_payload['cancel_report'])

    ###########################################################################
    #                                                                         #
    #                                  LOW LEVEL                              #
    #                                                                         #
    ###########################################################################

    async def _perform_request(self, service, message):
        logger.debug(f"Client is sending {message}")
        url = f"{self.vtn_url}/{service}"
        try:
            async with self.client_session.post(url, data=message) as req:
                if req.status != HTTPStatus.OK:
                    logger.warning(f"Non-OK status when performing a request "
                                   f"to {url} with data {message}: {req.status}")
                    return None, {}
                content = await req.read()
                logger.debug(content.decode('utf-8'))
        except aiohttp.client_exceptions.ClientConnectorError as err:
            # Could not connect to server
            logger.error(f"Could not connect to server with URL {self.vtn_url}:")
            logger.error(f"{err.__class__.__name__}: {str(err)}")
            return None, {}
        except Exception as err:
            breakpoint()
        try:
            message_type, message_payload = self._parse_message(content)
        except Exception as err:
            logger.error(f"The incoming message could not be parsed or validated: {content}.")
            raise err
            return None, {}
        return message_type, message_payload

    async def _on_event(self, message):
        logger.debug(f"The VEN received an event")
        result = self.on_event(message)
        if asyncio.iscoroutine(result):
            result = await result

        logger.debug(f"Now responding with {result}")
        request_id = message['request_id']
        event_id = message['events'][0]['event_descriptor']['event_id']
        await self.created_event(request_id, event_id, result)
        return

    async def _poll(self):
        logger.debug("Now polling for new messages")
        response_type, response_payload = await self.poll()
        if response_type is None:
            return

        if response_type == 'oadrResponse':
            logger.debug("No events or reports available")
            return

        if response_type == 'oadrRequestReregistration':
            logger.info("The VTN required us to re-register. Calling the registration procedure.")
            await self.create_party_registration()

        if response_type == 'oadrDistributeEvent':
            await self._on_event(response_payload)

        elif response_type == 'oadrUpdateReport':
            await self._on_report(response_payload)

        else:
            logger.warning(f"No handler implemented for incoming message "
                           f"of type {response_type}, ignoring.")

        # Immediately poll again, because there might be more messages
        await self._poll()
