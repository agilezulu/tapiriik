import requests
import logging
import json
import dateutil.parser
from collections import defaultdict
from datetime import datetime, timedelta
from django.core.urlresolvers import reverse

from urllib.parse import urlencode

from tapiriik.settings import WEB_ROOT, RUNPLAN_CLIENT_SECRET, RUNPLAN_CLIENT_ID
from tapiriik.services.service_base import ServiceAuthenticationType, ServiceBase
from tapiriik.services.service_record import ServiceRecord
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit, \
    Waypoint, WaypointType, Location, Lap, LapIntensity
from tapiriik.services.api import APIException, UserException, UserExceptionType, APIExcludeActivity

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
            raise APIException("Unable to deauthorize Runplan auth token, status: {} resp: {}".format(resp.status_code, resp.text))
        pass

    def DownloadActivityList(self, serviceRecord, exhaustive=False):
        activities = []
        exclusions = []

        resp = requests.get(
            "{}/api/p1/sync/activities".format(self.runplan_url),
            headers=self._apiHeaders(serviceRecord.Authorization)
        )

        try:
            act_list = resp.json().get("data")

            for act in act_list:
                activity = UploadedActivity()
                activity.StartTime = dateutil.parser.parse(act.get("startDateTimeLocal"))
                activity.EndTime = dateutil.parser.parse(act.get("endDateTimeLocal"))
                _type = self._activityMappings.get(act.get("activityType"))
                if not _type:
                    exclusions.append(
                        APIExcludeActivity(
                            "Unsupported activity type {}".format(act.get("activityType")),
                            activity_id=act.get("activityId"),
                            user_exception=UserException(UserExceptionType.Other)
                        )
                    )
                activity.ServiceData = {"ActivityID": act.get("activityId")}
                activity.Type = _type
                activity.Notes = act.get("activityNotes")
                activity.Name = act.get("activityName")
                activity.GPS = bool(act.get("startLatitude"))
                activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Kilometers, value=act.get("distanceKm"))
                activity.Stats.Energy = ActivityStatistic(ActivityStatisticUnit.Kilocalories, value=act.get("calories"))

                if "heartRateMin" in act:
                    activity.Stats.HR = ActivityStatistic(
                        ActivityStatisticUnit.BeatsPerMinute,
                        min=act.get("heartRateMin"), max=act.get("heartRateMax"), avg=act.get("heartRateAverage"))

                activity.Stats.MovingTime = ActivityStatistic(ActivityStatisticUnit.Seconds, value=act.get("durationSeconds"))

                if "temperature" in act:
                    activity.Stats.Temperature = ActivityStatistic(
                        ActivityStatisticUnit.DegreesCelcius, avg=act.get("temperature"))

                activity.CalculateUID()
                logger.debug("\tActivity s/t %s", activity.StartTime)
                activities.append(activity)

            return activities, exclusions

        except ValueError:
            raise APIException("Error decoding activity list resp {} {}".format(resp.status_code, resp.text))

    def DownloadActivity(self, serviceRecord, activity):
        activity_id = activity.ServiceData.get("ActivityID")

        resp = requests.get(
            "{}/api/p1/sync/activity/{}".format(self.runplan_url, activity_id),
            headers=self._apiHeaders(serviceRecord.Authorization)
        )
        try:
            act = resp.json().get("data")

            recordingKeys = act.get('recordingKeys')

            if act.get('source') == 'manual' or not recordingKeys:
                # it's a manually entered run, can't get much info
                activity.Stationary = True
                activity.Laps = [Lap(startTime=activity.StartTime, endTime=activity.EndTime, stats=activity.Stats)]
                return activity

            activity.Stationary = False

            if not act.get('laps'):
                # no laps, just make one big lap
                activity.Laps = [Lap(startTime=activity.StartTime, endTime=activity.EndTime, stats=activity.Stats)]

            startTime = activity.StartTime
            for lapRecord in act.get('laps'):
                endTime = activity.StartTime + timedelta(seconds=lapRecord['endDuration'])
                lap = Lap(startTime=startTime, endTime=endTime)
                activity.Laps.append(lap)
                startTime = endTime + timedelta(seconds=1)

            for value in zip(*act['recordingValues']):
                record = dict(zip(recordingKeys, value))
                ts = activity.StartTime + timedelta(seconds=record['clock'])
                location = None
                if 'latitude' in record:
                    alt = record.get('elevation')
                    lat = record['latitude']
                    lon = record['longitude']
                    # Smashrun seems to replace missing measurements with -1
                    if lat == -1:
                        lat = None
                    if lon == -1:
                        lon = None
                    location = Location(lat=lat, lon=lon, alt=alt)
                hr = record.get('heartRate')
                runCadence = record.get('cadence')
                temp = record.get('temperature')
                distance = record.get('distance') * 1000
                wp = Waypoint(timestamp=ts, location=location, hr=hr,
                              runCadence=runCadence, temp=temp,
                              distance=distance)
                # put the waypoint inside the lap it corresponds to
                for lap in activity.Laps:
                    if lap.StartTime <= wp.Timestamp <= lap.EndTime:
                        lap.Waypoints.append(wp)
                        break

            return activity

        except ValueError:
            raise APIException("Error fetching activity code: {} error: {}".format(resp.status_code, resp.text))

    def _resolveDuration(self, obj):
        if obj.Stats.TimerTime.Value is not None:
            return obj.Stats.TimerTime.asUnits(ActivityStatisticUnit.Seconds).Value
        if obj.Stats.MovingTime.Value is not None:
            return obj.Stats.MovingTime.asUnits(ActivityStatisticUnit.Seconds).Value
        return (obj.EndTime - obj.StartTime).total_seconds()

    def _createActivity(self, serviceRecord, data):
        resp = requests.post(
            "{}/api/p1/sync/activity/upload".format(self.runplan_url),
            data={'activity': json.dumps(data)},
            headers=self._apiHeaders(serviceRecord.Authorization)
        )
        return resp.json().get("data")

    def UploadActivity(self, serviceRecord, activity):
        data = {}
        data['provider'] = "Tapiriik"
        data['activityId'] = activity.UID
        data['startDateTimeLocal'] = activity.StartTime.isoformat()
        data['distance'] = activity.Stats.Distance.asUnits(ActivityStatisticUnit.Kilometers).Value
        data['duration'] = self._resolveDuration(activity)
        data['activityType'] = self._reverseActivityMappings.get(activity.Type)

        def setIfNotNone(d, k, *vs, f=lambda x: x):
            for v in vs:
                if v is not None:
                    d[k] = f(v)
                    return

        setIfNotNone(data, 'notes', activity.Notes, activity.Name)
        setIfNotNone(data, 'cadenceAverage', activity.Stats.RunCadence.Average, f=int)
        setIfNotNone(data, 'cadenceMin', activity.Stats.RunCadence.Min, f=int)
        setIfNotNone(data, 'cadenceMax', activity.Stats.RunCadence.Max, f=int)
        setIfNotNone(data, 'heartRateAverage', activity.Stats.HR.Average, f=int)
        setIfNotNone(data, 'heartRateMin', activity.Stats.HR.Min, f=int)
        setIfNotNone(data, 'heartRateMax', activity.Stats.HR.Max, f=int)
        setIfNotNone(data, 'temperatureAverage', activity.Stats.Temperature.Average)

        if not activity.Laps[0].Waypoints:
            # no info, no need to go further
            return self._createActivity(serviceRecord, data)

        data['laps'] = []
        recordings = defaultdict(list)

        def getattr_nested(obj, attr):
            attrs = attr.split('.')
            while attrs:
                r = getattr(obj, attrs.pop(0), None)
                obj = r
            return r

        def hasStat(activity, stat):
            for lap in activity.Laps:
                for wp in lap.Waypoints:
                    if getattr_nested(wp, stat) is not None:
                        return True
            return False

        hasDistance = hasStat(activity, 'Distance')
        hasTimestamp = hasStat(activity, 'Timestamp')
        hasLatitude = hasStat(activity, 'Location.Latitude')
        hasLongitude = hasStat(activity, 'Location.Longitude')
        hasAltitude = hasStat(activity, 'Location.Altitude')
        hasHeartRate = hasStat(activity, 'HR')
        hasCadence = hasStat(activity, 'RunCadence')
        hasTemp = hasStat(activity, 'Temp')

        for lap in activity.Laps:
            lapinfo = {
                'lapType': self._intensityMappings.get(lap.Intensity, 'general'),
                'endDuration': (lap.EndTime - activity.StartTime).total_seconds(),
                'endDistance': lap.Waypoints[-1].Distance / 1000
            }
            data['laps'].append(lapinfo)
            for wp in lap.Waypoints:
                if hasDistance:
                    recordings['distance'].append(wp.Distance / 1000)
                if hasTimestamp:
                    clock = (wp.Timestamp - activity.StartTime).total_seconds()
                    recordings['clock'].append(int(clock))
                if hasLatitude:
                    recordings['latitude'].append(wp.Location.Latitude)
                if hasLongitude:
                    recordings['longitude'].append(wp.Location.Longitude)
                if hasAltitude:
                    recordings['elevation'].append(wp.Location.Altitude)
                if hasHeartRate:
                    recordings['heartRate'].append(wp.HR)
                if hasCadence:
                    recordings['cadence'].append(wp.RunCadence)
                if hasTemp:
                    recordings['temperature'].append(wp.Temp)

        data['recordingKeys'] = sorted(recordings.keys())
        data['recordingValues'] = [recordings[k] for k in data['recordingKeys']]
        assert len(set(len(v) for v in data['recordingValues'])) == 1

        return self._createActivity(serviceRecord, data)

    def DeleteCachedData(self, serviceRecord):
        pass  # No cached data...
