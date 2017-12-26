from tapiriik.settings import WEB_ROOT, RUNPLAN_CLIENT_SECRET, RUNPLAN_CLIENT_ID
from tapiriik.services.service_base import ServiceAuthenticationType, ServiceBase
from tapiriik.services.service_record import ServiceRecord
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit, \
    Waypoint, WaypointType, Location, Lap
from tapiriik.services.api import APIException, UserException, UserExceptionType, APIExcludeActivity
from tapiriik.services.fit import FITIO
from tapiriik.services.tcx import TCXIO

from django.core.urlresolvers import reverse
from datetime import datetime, timedelta
from urllib.parse import urlencode
import calendar
import dateutil.parser
import requests
import os
import logging
import pytz
import re
import time
import json

logger = logging.getLogger(__name__)

class RunplanService(ServiceBase):
    # XXX need to normalise API paths - some url contains additional /api as direct to main server

    ID = "runplan"
    DisplayName = "Runplan"
    DisplayAbbreviation = "RP"
    AuthenticationType = ServiceAuthenticationType.OAuth
    AuthenticationNoFrame = True # iframe too small
    LastUpload = None
    #runplan_url = "https://runplan.training"
    #runplan_url = "http://localhost:8010"
    runplan_url = "http://10.0.2.2:8010"

    SupportsHR = SupportsCadence = SupportsTemp = SupportsPower = True

    SupportsActivityDeletion = False

    SupportedActivities = ActivityType.List() # All

    def UserUploadedActivityURL(self, uploadId):
        raise NotImplementedError
        # XXX need to include user id
        # return self.runplan_url + "/activities/view?targetUserId=%s&activityId=%s" % uploadId

    def WebInit(self):
        params = {
            "scope": "sync",
            "client_id": RUNPLAN_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": WEB_ROOT + reverse("oauth_return", kwargs={"service": "runplan"})
        }
        #self.UserAuthorizationURL = self.runplan_url + "/oauth/authorise?" + urlencode(params)
        self.UserAuthorizationURL = "http://localhost:8010/oauth/authorise?" + urlencode(params)

    def _apiHeaders(self, authorization):
        return {"Authorization": "Bearer " + authorization["OAuthToken"]}

    def RetrieveAuthorizationToken(self, req, level):
        code = req.GET.get("code")
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": RUNPLAN_CLIENT_ID,
            "client_secret": RUNPLAN_CLIENT_SECRET,
            "redirect_uri": WEB_ROOT + reverse("oauth_return", kwargs={"service": "runplan"})
        }
        #url = "http://10.0.2.2:8010/oauth/token"
        url = "{}/oauth/token".format(self.runplan_url)

        print('REQUEST >>>>> ', url)
        response = requests.post(url, data=params)

        print('RESPONSE >>> ', response.text)

        if response.status_code != 200:
            raise APIException("Invalid code")
        data = response.json()

        authorizationData = {"OAuthToken": data["access_token"]}

        id_resp = requests.get("{}/api/sync/user".format(self.runplan_url), headers=self._apiHeaders(authorizationData))
        return (id_resp.json()["id"], authorizationData)



    def RevokeAuthorization(self, serviceRecord):
        resp = requests.post(
            self.runplan_url + "/api/oauth/revoke",
            data={"token": serviceRecord.Authorization["OAuthToken"]},
            headers=self._apiHeaders(serviceRecord.Authorization)
        )
        if resp.status_code != 204 and resp.status_code != 200:
            raise APIException("Unable to deauthorize TAO auth token, status " + str(resp.status_code) + " resp " + resp.text)
        pass

    def DownloadActivityList(self, serviceRecord, exhaustive=False):
        allItems = []

        if exhaustive:
            pageUri = self.runplan_url + "/api/sync/activities?pageSize=200"
        else:
            pageUri = self.runplan_url + "/api/sync/activities"

        while True:
            response = requests.get(pageUri,  headers=self._apiHeaders(serviceRecord.Authorization))
            if response.status_code != 200:
                if response.status_code == 401 or response.status_code == 403:
                    raise APIException(
                        "No authorization to retrieve activity list",
                        block=True,
                        user_exception=UserException(UserExceptionType.Authorization,
                        intervention_required=True)
                    )
                raise APIException("Unable to retrieve activity list " + str(response) + " " + response.text)
            data = response.json()
            allItems += data["activities"]
            if not exhaustive or "next" not in data or data["next"] is None:
                break
            pageUri = self.runplan_url + data["next"]

        activities = []
        exclusions = []
        for act in allItems:
            try:
                activity = self._populateActivity(act)
            except KeyError as e:
                exclusions.append(
                    APIExcludeActivity("Missing key in activity data " + str(e),
                    activity_id=act["activityId"],
                    user_exception=UserException(UserExceptionType.Corrupt))
                )
                continue

            logger.debug("\tActivity s/t " + str(activity.StartTime))
            activity.ServiceData = {"id": act["activityId"]}
            activities.append(activity)
        return activities, exclusions

    def _populateActivity(self, rawRecord):
        ''' Populate the 1st level of the activity object with all details required for UID from  API data '''
        activity = UploadedActivity()
        activity.StartTime = dateutil.parser.parse(rawRecord["start"])
        activity.EndTime = activity.StartTime + timedelta(seconds=rawRecord["duration"])
        activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Meters, value=rawRecord["distance"])
        activity.GPS = rawRecord["hasGps"]
        activity.Stationary = not rawRecord["hasGps"]
        activity.CalculateUID()
        return activity

    def DownloadActivity(self, serviceRecord, activity):
        activity_id = activity.ServiceData["id"]
        # Switch URL to /api/sync/activity/fit/ once FITIO.Parse() available
        resp = requests.get(
            self.runplan_url + "/api/sync/activity/tcx/" + activity_id,
            headers=self._apiHeaders(serviceRecord.Authorization)
        )

        try:
            TCXIO.Parse(resp.content, activity)
        except ValueError as e:
            raise APIExcludeActivity("TCX parse error " + str(e), user_exception=UserException(UserExceptionType.Corrupt))

        return activity

    def UploadActivity(self, serviceRecord, activity):
        # Upload the workout as a .FIT file
        uploaddata = FITIO.Dump(activity)

        headers = self._apiHeaders(serviceRecord.Authorization)
        headers['Content-Type'] = 'application/octet-stream'
        resp = requests.post(self.runplan_url + "/api/sync/activity/fit", data=uploaddata, headers=headers)

        if resp.status_code != 200:
            raise APIException(
                "Error uploading activity - " + str(resp.status_code),
                block=False)

        responseJson = resp.json()

        if not responseJson["id"]:
            raise APIException(
                "Error uploading activity - " + resp.Message,
                block=False)

        activityId = responseJson["id"]

        return activityId

    def DeleteCachedData(self, serviceRecord):
        pass  # No cached data...
