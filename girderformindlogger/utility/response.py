import backports
import isodate
import itertools
import pandas as pd
import pytz
import tzlocal
from backports.datetime_fromisoformat import MonkeyPatch
from bson.codec_options import CodecOptions
from bson.objectid import ObjectId
from datetime import date, datetime, timedelta
from girderformindlogger.models.applet import Applet as AppletModel
from girderformindlogger.models.user import User as UserModel
from girderformindlogger.models.response_folder import ResponseItem
from girderformindlogger.utility import clean_empty
from pandas.api.types import is_numeric_dtype
from pymongo import ASCENDING, DESCENDING
MonkeyPatch.patch_fromisoformat()


def getSchedule(currentUser, timezone=None):
    from .jsonld_expander import formatLdObject
    return({
        applet['applet'].get('_id', ''): {
            applet['activities'][activity].get('_id', ''): {
                'lastResponse': getLatestResponseTime(
                    currentUser['_id'],
                    applet['applet']['_id'].split('applet/')[-1],
                    activity,
                    tz=timezone
                ) #,
                # 'nextScheduled': None,
                # 'lastScheduled': None
            } for activity in list(
                applet.get('activities', {}).keys()
            )
        } for applet in [
            formatLdObject(
                applet,
                'applet',
                currentUser
            ) for applet in AppletModel().getAppletsForUser(
                user=currentUser,
                role='user'
            )
        ]
    })


def getLatestResponse(informantId, appletId, activityURL):
    from .jsonld_expander import reprolibCanonize, reprolibPrefix
    responses = list(ResponseItem().find(
        query={
            "baseParentType": 'user',
            "baseParentId": informantId if isinstance(
                informantId,
                ObjectId
            ) else ObjectId(informantId),
            "meta.applet.@id": {
                "$in": [
                    appletId,
                    ObjectId(appletId)
                ]
            },
            "meta.activity.url": {
                "$in": [
                    activityURL,
                    reprolibPrefix(activityURL),
                    reprolibCanonize(activityURL)
                ]
            }
        },
        force=True,
        sort=[("updated", DESCENDING)]
    ))
    if len(responses):
        return(responses[0])
    return(None)


def getLatestResponseTime(informantId, appletId, activityURL, tz=None):
    latestResponse = getLatestResponse(informantId, appletId, activityURL)
    try:
        latestResponse['updated'].isoformat(
        ) if tz is None else latestResponse['updated'].astimezone(pytz.timezone(
            tz
        )).isoformat()
    except TypeError:
        pass
    except:
        import sys, traceback
        print(sys.exc_info())
        print(traceback.print_tb(sys.exc_info()[2]))
    return(
        (
            latestResponse['updated'].astimezone(pytz.timezone(
                tz
            )).isoformat() if (
                isinstance(tz, str) and tz in pytz.all_timezones
            ) else latestResponse['updated'].isoformat()
        ) if (
            isinstance(latestResponse, dict) and isinstance(
                latestResponse.get('updated'),
                datetime
            )
        ) else None
    )


def aggregate(metadata, informant, startDate=None, endDate=None, getAll=False):
    """
    Function to calculate aggregates
    """
    thisResponseTime = datetime.now(
        tzlocal.get_localzone()
    )

    startDate = datetime.fromisoformat(startDate.isoformat(
    )).astimezone(pytz.utc).replace(tzinfo=None) if startDate is not None else None

    endDate = datetime.fromisoformat((
        thisResponseTime if endDate is None else endDate
    ).isoformat()).astimezone(pytz.utc).replace(tzinfo=None)

    query = {
            "baseParentType": 'user',
            "baseParentId": informant.get("_id") if isinstance(
                informant,
                dict
            ) else informant,
            "updated": {
                "$gte": startDate,
                "$lt": endDate
            } if startDate else {
                "$lt": endDate
            },
            "meta.applet.@id": metadata.get("applet", {}).get("@id"),
            "meta.activity.url": metadata.get("activity", {}).get("url"),
            "meta.subject.@id": metadata.get("subject", {}).get("@id")
        }

    definedRange = list(ResponseItem().find(
        query=query,
        force=True,
        sort=[("updated", ASCENDING)]
    ))

    if not len(definedRange):
        # TODO: I'm afraid of some asynchronous database writes
        # that sometimes make defined range an empty list.
        # For now I'm exiting, but this needs to be looked
        # into.
        print('\n\n defined range returns an empty list.')
        return
        # raise ValueError("The defined range doesn't have a length")

    startDate = min([response.get(
        'updated',
        endDate
    ) for response in definedRange]) if startDate is None else startDate

    duration = isodate.duration_isoformat(
        delocalize(endDate) - delocalize(startDate)
    )

    responseIRIs = _responseIRIs(definedRange)
    for itemIRI in responseIRIs:
        for response in definedRange:
            if itemIRI in response.get(
                'meta',
                {}
            ).get('responses', {}):
                completedDate(response)

    aggregated = {
        "schema:startDate": startDate,
        "schema:endDate": endDate,
        "schema:duration": duration,
        "responses": {
            itemIRI: [
                {
                    "value": response.get('meta', {}).get('responses', {}).get(
                        itemIRI
                    ),
                    "date": completedDate(response)
                } for response in definedRange if itemIRI in response.get(
                    'meta',
                    {}
                ).get('responses', {})
            ] for itemIRI in responseIRIs
        } if getAll else countResponseValues(definedRange, responseIRIs)
    }
    return(aggregated)


def completedDate(response):
    completed = response.get("updated", {})
    return completed


def formatResponse(response):
    try:
        metadata = response.get('meta', response)
        if any([
            key not in metadata.keys() for key in [
                'allTime',
                'last7Days'
            ]
        ]):
            aggregateAndSave(response, response.get('baseParentId'))
        thisResponse = {
            "thisResponse": {
                "schema:startDate": isodatetime(
                    metadata.get(
                        'responseStarted',
                        response.get(
                            'updated',
                            datetime.now()
                        )
                    )
                ),
                "schema:endDate": isodatetime(
                    metadata.get(
                        'responseCompleted',
                        response.get(
                            'updated',
                            datetime.now()
                        )
                    )
                ),
                "responses": {
                    itemURI: metadata['responses'][
                        itemURI
                    ] for itemURI in metadata.get('responses', {})
                }
            },
              "allToDate": metadata.get("allTime"),
              "last7Days": metadata.get("last7Days")
        } if isinstance(metadata, dict) and all([
            key in metadata.keys() for key in [
                'responses',
                'applet',
                'activity',
                'subject'
            ]
        ]) else {}
    except Exception as e:
        import sys, traceback
        print(sys.exc_info())
        print(traceback.print_tb(sys.exc_info()[2]))
        thisResponse = None
    return(clean_empty(thisResponse))


def string_or_ObjectID(s):
    return([str(s), ObjectId(s)])


def _responseIRIs(definedRange):
    return(list(set(itertools.chain.from_iterable([list(
        response.get('meta', {}).get('responses', {}).keys()
    ) for response in definedRange if isinstance(response, dict)]))))


def _flattenDF(df, columnName):
    if isinstance(columnName, list):
        for c in columnName:
            df = _flattenDF(df, c)
        return(df)
    prefix = columnName if columnName not in ['meta', 'responses'] else ""
    newDf = pd.concat(
        [
            df[columnName].apply(
                pd.Series
            ),
            df.drop(columnName, axis=1)
        ],
        axis=1
    )
    return(
        (
            newDf.rename(
                {
                    col: "{}-{}".format(
                        prefix,
                        col
                    ) for col in list(
                        df[columnName][0].keys()
                    )
                },
                axis='columns'
            ) if len(prefix) else newDf
        ).dropna('columns', 'all')
    )


def countResponseValues(definedRange, responseIRIs=None):
    responseIRIs = _responseIRIs(
        definedRange
    ) if responseIRIs is None else responseIRIs
    pd.set_option('display.max_colwidth', -1)
    pd.set_option('display.max_columns', None)
    df = pd.DataFrame(definedRange)
    df = _flattenDF(df, ['meta', 'applet', 'activity', 'responses'])
    counts = {
        responseIRI: (
            df[responseIRI].astype(str) if not(is_numeric_dtype(
                df[responseIRI]
            )) else df[responseIRI]
        ).value_counts().to_dict(
        ) for responseIRI in responseIRIs if isinstance(
            df[responseIRI],
            pd.Series
        )
    }
    return(
        {
            responseIRI: [
                {
                    "value": value,
                    "count": counts[responseIRI][value]
                } for value in counts[responseIRI]
            ] for responseIRI in counts
        }
    )


def delocalize(dt):
    print("delocalizing {} ({}; {})".format(
        dt,
        type(dt),
        dt.tzinfo if isinstance(dt, datetime) else ""
    ))
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return(dt)
        print(dt.astimezone(pytz.utc).replace(
            tzinfo=None
        ))
        return(dt.astimezone(pytz.utc).replace(
            tzinfo=None
        ))
    elif isinstance(dt, str):
        return(datetime.fromisoformat(dt).astimezone(pytz.utc).replace(
            tzinfo=None
        ))
    print("Here's the problem: {}".format(dt))
    raise TypeError


def aggregateAndSave(item, informant):
    if item == {} or item is None:
        return({})
    metadata = item.get("meta", {})
    # Save 1 (of 3)
    if metadata and metadata != {}:
        item = ResponseItem().setMetadata(item, metadata)
    # sevenDay ...
    metadata = item.get("meta", {})
    endDate = datetime.now(
        tzlocal.get_localzone()
    )
    startDate = (endDate - timedelta(days=7)).date()
    print("From {} to {}".format(
        startDate.strftime("%c"),
        endDate.strftime("%c")
    ))
    metadata["last7Days"] = aggregate(
        metadata,
        informant,
        startDate=startDate,
        endDate=endDate,
        getAll=True
    )

    # save (2 of 3)
    if metadata and metadata != {}:
        item = ResponseItem().setMetadata(item, metadata)
    # allTime
    metadata = item.get("meta", {})
    metadata["allTime"] = aggregate(
        metadata,
        informant,
        endDate=endDate,
        getAll=False
    )

    # save (3 of 3)
    if metadata and metadata != {}:
        item = ResponseItem().setMetadata(item, metadata)
    return(item)


def last7Days(
    appletId,
    appletInfo,
    informantId,
    reviewer,
    subject=None,
    referenceDate=None
):
    from bson import json_util
    from .jsonld_expander import loadCache, reprolibCanonize, reprolibPrefix
    referenceDate = delocalize(
        datetime.now(
            tzlocal.get_localzone()
        ) if referenceDate is None else referenceDate # TODO allow timeless dates
    )

    # we need to get the activities
    cachedApplet = loadCache(appletInfo['cached'])
    listOfActivities = [
        reprolibPrefix(activity) for activity in list(
            cachedApplet['activities'].keys()
        )
    ]

    getLatestResponsesByAct = lambda activityURI: list(ResponseItem().find(
        query={
            "baseParentType": 'user',
            "baseParentId": informantId if isinstance(
                informantId,
                ObjectId
            ) else ObjectId(informantId),
            "updated": {
                "$lte": referenceDate
            },
            "meta.applet.@id": {
                "$in": [
                    appletId,
                    ObjectId(appletId)
                ]
            },
            "meta.activity.url": {
                "$in": [
                    activityURI,
                    reprolibPrefix(activityURI),
                    reprolibCanonize(activityURI)
                ]
            }
        },
        force=True,
        sort=[("updated", DESCENDING)]
    ))

    latestResponses = [getLatestResponsesByAct(act) for act in listOfActivities]

    # destructure the responses
    # TODO: we are assuming here that activities don't share items.
    # might not be the case later on, so watch out.

    outputResponses = {}

    for resp in latestResponses:
        if len(resp):
            latest = resp[0]

            # the last 7 days for the most recent entry for the activity
            l7 = latest.get('meta', {}).get('last7Days', {}).get('responses', {})

            # the current response for the most recent entry for the activity
            currentResp = latest.get('meta', {}).get('responses', {})

            # update the l7 with values from currentResp
            for (key, val) in currentResp.items():
                if key in l7.keys():
                    l7[key].append(dict(date=latest['updated'], value=val))
                else:
                    l7[key] = [dict(date=latest['updated'], value=val)]

            outputResponses.update(l7)

    l7d = {}
    l7d["responses"] = _oneResponsePerDate(outputResponses)
    endDate = referenceDate.date()
    l7d["schema:endDate"] = endDate.isoformat()
    startDate = endDate - timedelta(days=7)
    l7d["schema:startDate"] = startDate.isoformat()
    l7d["schema:duration"] = isodate.duration_isoformat(
        endDate - startDate
    )
    return l7d



def determine_date(d):
    if isinstance(d, int):
        while (d > 10000000000):
            d = d/10
        d = datetime.fromtimestamp(d)
    return((
        datetime.fromisoformat(
            d
        ) if isinstance(d, str) else d
    ).date())


def isodatetime(d):
    if isinstance(d, int):
        while (d > 10000000000):
            d = d/10
        d = datetime.fromtimestamp(d)
    return((
        datetime.fromisoformat(
            d
        ) if isinstance(d, str) else d
    ).isoformat())


def responseDateList(appletId, userId, reviewer):
    from girderformindlogger.models.profile import Profile
    userId = ProfileModel().getProfile(userId, reviewer)
    if not isinstance(userId, dict):
        return([])
    userId = userId.get('userId')
    rdl = list(set([
        determine_date(
            response.get("meta", {}).get(
                "responseCompleted",
                response.get("updated")
            )
        ).isoformat() for response in list(ResponseItem().find(
            query={
                "baseParentType": 'user',
                "baseParentId": userId,
                "meta.applet.@id": appletId
            },
            sort=[("updated", DESCENDING)]
        ))
    ]))
    rdl.sort(reverse=True)
    return(rdl)


def _oneResponsePerDate(responses):
    newResponses = {}
    for response in responses:
        df = pd.DataFrame(responses[response])
        df["datetime"] = df.date
        df["date"] = df.date.apply(determine_date)
        df.sort_values(by=['datetime'], ascending=False, inplace=True)
        df = df.groupby('date').first()
        df.drop('datetime', axis=1, inplace=True)
        df['date'] = df.index
        newResponses[response] = df.to_dict(orient="records")
    return(newResponses)
