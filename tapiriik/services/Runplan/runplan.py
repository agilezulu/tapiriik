from tapiriik.settings import WEB_ROOT, RUNPLAN_CLIENT_SECRET, RUNPLAN_CLIENT_ID
from tapiriik.services.service_base import ServiceAuthenticationType, ServiceBase
from tapiriik.services.service_record import ServiceRecord
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit, \
    Waypoint, WaypointType, Location, Lap, LapIntensity
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

    SupportedActivities = [ActivityType.Running]
    SupportsHR = SupportsCalories = SupportsCadence = SupportsTemp = True
    SupportsActivityDeletion = False

    _reverseActivityMappings = {
        ActivityType.Running: "running",
    }
    _activityMappings = {
        "running": ActivityType.Running,
    }

    _intensityMappings = {
        LapIntensity.Active: 'work',
        LapIntensity.Rest: 'recovery',
        LapIntensity.Warmup: 'warmup',
        LapIntensity.Cooldown: 'cooldown',
    }

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
        response = requests.post("{}/oauth/token".format(self.runplan_url), data=params)

        if response.status_code != 200:
            raise APIException("Invalid code")
        data = response.json()

        authorizationData = {"OAuthToken": data.get("access_token")}

        reponse_uuid = requests.get(
            "{}/api/p1/sync/user/uuid".format(self.runplan_url),
            headers=self._apiHeaders(authorizationData)
        )
        if response.status_code != 200:
            raise APIException("Invalid call to user")

        user_uuid = reponse_uuid.json().get("uuid")

        return user_uuid, authorizationData


    def RevokeAuthorization(self, serviceRecord):

        resp = requests.post(
            "{}/oauth/revoke".format(self.runplan_url),
            data={"token": serviceRecord.Authorization.get("OAuthToken"), "client_id": RUNPLAN_CLIENT_ID},
            headers=self._apiHeaders(serviceRecord.Authorization)
        )

        if resp.status_code != 204 and resp.status_code != 200:
            raise APIException("Unable to deauthorize Runplan auth token, status " + str(resp.status_code) + " resp " + resp.text)
        pass

    def DownloadActivityList(self, serviceRecord, exhaustive=False):
        activities = []
        exclusions = []

        resp = requests.get(
            "{}/api/p1/sync/activities".format(self.runplan_url),
            headers=self._apiHeaders(serviceRecord.Authorization)
        )

        try:
            act_list = resp.json()["data"]

            for act in act_list:
                activity = UploadedActivity()
                activity.StartTime = dateutil.parser.parse(act['startDateTimeLocal'])
                activity.EndTime = activity.StartTime + timedelta(seconds=act['duration'])
                _type = self._activityMappings.get(act['activityType'])
                if not _type:
                    exclusions.append(APIExcludeActivity("Unsupported activity type %s" % act['activityType'],
                                                         activity_id=act["activityId"],
                                                         user_exception=UserException(UserExceptionType.Other)))
                activity.ServiceData = {"ActivityID": act['activityId']}
                activity.Type = _type
                activity.Notes = act['notes']
                activity.GPS = bool(act.get('startLatitude'))
                activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Kilometers, value=act['distance'])
                activity.Stats.Energy = ActivityStatistic(ActivityStatisticUnit.Kilocalories, value=act['calories'])
                if 'heartRateMin' in act:
                    activity.Stats.HR = ActivityStatistic(ActivityStatisticUnit.BeatsPerMinute, min=act['heartRateMin'],
                                                          max=act['heartRateMax'], avg=act['heartRateAverage'])
                activity.Stats.MovingTime = ActivityStatistic(ActivityStatisticUnit.Seconds, value=act['duration'])

                if 'temperature' in act:
                    activity.Stats.Temperature = ActivityStatistic(ActivityStatisticUnit.DegreesCelcius,
                                                                   avg=act['temperature'])
                activity.CalculateUID()
                logger.debug("\tActivity s/t %s", activity.StartTime)
                activities.append(activity)

            return activities, exclusions

        except ValueError:
            self._rateLimitBailout(resp)
            raise APIException("Error decoding activity list resp %s %s" % (resp.status_code, resp.text))

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
