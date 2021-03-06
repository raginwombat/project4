#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'

import logging

from datetime import datetime
import time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import TeeShirtSize
from models import Session
from models import SessionForm
from models import SessionForms
from models import WishList

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FEATURED_SPEAKERS_KEY = "FEATURED_SPEAKERS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

SESS_DEFAULT = {
    
    'highlights': 'Default',
    'location': 'Default',
    'typeofSession': [],
    'speakers': [],
    'startDate': '1900-01-01',
    'startTime': '00:00',
    'endTime': '00:00',
    'endDate': '1900-01-01',
    'maxAttendees': 0,
    'seatsAvailable': 0
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_GET_REQ = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

SESS_GET_REQ_TYPE = endpoints.ResourceContainer(
    websafeConferenceKey=messages.StringField(1),
    sessionType = messages.StringField(2)
)

SESS_GET_REQ_SPEAK = endpoints.ResourceContainer(
    speakers = messages.StringField(1, repeated=True)
)

SESS_CREATE_REQ = endpoints.ResourceContainer(
    name            = messages.StringField(1, required=True),
    highlights      = messages.StringField(2),
    location        = messages.StringField(3),
    typeofSession   = messages.StringField(4, repeated=True),
    speakers        = messages.StringField(5, repeated=True ),
    startDate       = messages.StringField(6),
    startTime       = messages.StringField(7),
    endTime         = messages.StringField(8),
    endDate         = messages.StringField(9),
    maxAttendees    = messages.IntegerField(10),
    seatsAvailable  = messages.IntegerField(11),
    #organizerUserId = messages.StringField(12),
    websafeConferenceKey=messages.StringField(12)
)

SESS_WISHLIST_REQ = endpoints.ResourceContainer(
    websafeSessionKey = messages.StringField(1)
)

SESS_GET_REQ_TIME = endpoints.ResourceContainer(
    searchTime             = messages.StringField(1),
    websafeConferenceKey    = messages.StringField(2)

)


TASK3_SOLUTION_REQ= endpoints.ResourceContainer(
    searchTime             = messages.StringField(1),
    sessionType    = messages.StringField(2, repeated=True)

)


# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        #if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        #else:
                        #    setattr(prof, field, val)
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Conference.city=="London")
        q = q.filter(Conference.topics=="Medical Innovations")
        q = q.filter(Conference.month==6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )
#########################################
#########################################


#########################################
#Task 1 add sessions to conference
#########################################

    @endpoints.method(SESS_CREATE_REQ, SessionForm,
        path='conference/{websafeConferenceKey}/session/create',
        name='createSession')

    def createSession(self, request):
        """Create New Session for Session"""
        return self._createSessionObject(request)

    def _createSessionObject(self, request):
        """ Create the session object from the request"""
         # preload necessary data items

        wsck = request.websafeConferenceKey


        '''Check that required information is provided '''
        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        if not wsck:
            raise endpoints.BadRequestException("Session 'websafeConferenceKey' field required")


        '''Check that user is authorized and conference key is valid'''
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        try:
            prof = self.authUserCheck()
        except:
            raise endpoints.UnauthorizedException('Authorization required')

        
        '''Confernce Creator Check checks if a user is authorized and the creator of the conference
         if they are it returns the confeence key'''
        try:
            c_key = self.conferenceCreatorCheck(wsck)
        except:
            raise endpoints.BadRequestException("Conference Key not valid")



        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}


        # add default values for those missing (both data model & outbound Message)
        for df in SESS_DEFAULT:
            "session value: %s"%df
            if data[df] in (None, []):
                data[df] = SESS_DEFAULT[df]
                #setattr(request, df, SESS_DEFAULT[df]) #debug code

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['startTime'] = datetime.strptime(data['startTime'], "%H:%M").time()
        
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()
            data['endTime'] = datetime.strptime(data['endTime'], "%H:%M").time()
 
        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]


        # generate Session Key based on  Conference ID
        s_id = Session.allocate_ids(size=1, parent=c_key)[0]
        s_key = ndb.Key(Session, s_id, parent=c_key)
        data['key'] = s_key
        
        
        '''Remove uneeded data'''
        del data['websafeConferenceKey']
   
        #Commit session to ndb
        Session(**data).put()

        #send email for notification of session creation
        taskqueue.add(params={'email': user.email(),
            'sessionInfo': repr(request)},
            url='/tasks/send_confirmation_email_session'
        )

        ################################################
        #Task 4 Call the getFeatured Speaker Task
        ################################################

        ''' Build the parameters list to be passed to the Task queue which will then be 
        passed to the helper method'''
        parameters =[('websafeConferenceKey', wsck),
                        ('sessionKey', s_key.urlsafe())]
        for speak in data['speakers']:
            parameters.append(('speaker', speak) )

        '''taskqueue.add(
            params= dict(parameters),
                url = '/tasks/featured_speaker_check'
                )'''

        taskqueue.add(
            params= dict( [('wsck', wsck), ('s_key', s_key.urlsafe()) ]),
                url = '/tasks/featured_speaker_check'
                )

        sess = s_key.get()
        return self._copySessionToForm(sess)


    def _copySessionToForm(self, sess):
        """Copy Session to SessionForm"""
        s = SessionForm()
        for field in s.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    date = getattr(sess, field.name)
                    setattr(s, field.name, date.strftime("%Y-%m-%d"))
                elif field.name.endswith('Time'):
                    time = getattr(sess, field.name)
                    setattr(s, field.name, time.strftime("%H:%M"))

                else:
                    setattr(s, field.name, getattr(sess, field.name))
        s.check_initialized()
        return s


    @endpoints.method(SESS_GET_REQ, SessionForms,
        path='getConferenceSessions',
        http_method = 'POST', 
        name = 'getConferenceSessions')
    def getConferenceSessions(self, request):
        """Return all sessions from a given conference."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)


        """Get Parent Conference obj"""
        conf_key = ndb.Key(urlsafe = request.websafeConferenceKey)

        sessns = Session.query(ancestor =conf_key)
        
        prof = ndb.Key(Profile, user_id).get()

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        )

    @endpoints.method(SESS_GET_REQ_TYPE, SessionForms,
        path='getConferenceSessionsByType',
        http_method = 'POST', 
        name = 'getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Return all sessions from a given conference with a specified type."""
        # make sure user is authed
        """Pass request parameters to helper method"""
        sessns = self._getConferenceSessionsByType(request.websafeConferenceKey, request.sessionType)

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        )

    def _getConferenceSessionsByType(self, websafeConferenceKey, sessionType):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
        """Get Parent Conference obj"""
        conf_key = ndb.Key(urlsafe = websafeConferenceKey)
        
        """Cast request as a tuple to use in the IN() filter """
        query_sessionTypes = [sessionType, '']
        
        sessns = Session.query(Session.typeofSession.IN(query_sessionTypes), ancestor = conf_key )

        return sessns

    

    @endpoints.method(SESS_GET_REQ_SPEAK, SessionForms,
        path='getSessionsBySpeaker',
        http_method = 'GET', 
        name = 'getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Return all sessions from a given conference with a specified type."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
                
        """Cast request as a tuple to use in the IN() filter """
        query_speakers = [s for s in request.speakers]
        sessns = Session.query(Session.speakers.IN(query_speakers))
        prof = ndb.Key(Profile, user_id).get()

        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        )


        


#########################################
#Task 2 Add sessions to user wishlist
#########################################

    @endpoints.method(SESS_WISHLIST_REQ, SessionForms,
        path='addSessionToWishlist',
        http_method='POST', name = 'addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Adds a session to a user's wishlist"""
        return self._wishlistRegistration(request)



    @ndb.transactional(xg=True)
    def _wishlistRegistration( self, request, save=True ):
        wssk = request.websafeSessionKey

        '''Check that user is authorized and conferenc key is valid'''
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        try:
            prof_key = self.authUserCheck()
        except:
            raise endpoints.UnauthorizedException('User not authorized in CreateSession')
        prof = prof_key.get()

        '''Check for valid session'''
        try:
            ndb.Key(urlsafe = wssk)
        except:
            raise endpoints.BadRequestException("Session Key isn't valid")

        '''Pull list of session keys from profile'''
        sessns = prof.sessionWishlistKeys

        """Check if wishlist  already contains the session"""
        if  wssk in prof.sessionWishlistKeys:
            '''Complete additon or removal based on Save flag'''
            if not save:
                '''If requestd session is in profile list remove it'''
                save_index = sessns.index(wssk) 
                sessns.pop(save_index)
        else:
            if save:
                sessns.append(wssk)

        '''Write saves back to profile'''
        prof.put()
            

        sessns = [ndb.Key(urlsafe = s_key).get() for s_key in prof.sessionWishlistKeys]


        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        )


    @endpoints.method(SESS_WISHLIST_REQ, SessionForms,
        path='deleteSessionFromWishlist',
        http_method='POST', name = 'deleteSessionFromWishlist')
    def deleteSessionFromWishlist(self, request):
        """Deletes a session from a user's wishlist"""

        return self._wishlistRegistration(request, save=False)


    @endpoints.method(message_types.VoidMessage, SessionForms,
        path = 'getSessionsInWishlist',
        http_method='GET', name = 'getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Gets a session to a user's wishlist"""
        """pull wishlist for user, if fails then create one and link to to the acncestor of the user"""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
        
        prof_key = ndb.Key(Profile, user_id)
        prof = prof_key.get()
        
        
        """Attempt to pull the wishlist"""
        sessns = [ndb.Key(urlsafe = s_key).get() for s_key in prof.sessionWishlistKeys]
        
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        )

#########################################
#Task 3 Additonal queries
#########################################


    @endpoints.method(SESS_GET_REQ_TIME, SessionForms,
        path='getSessionsBeforeTime',
        http_method='GET', name = 'getSessionsBeforeTime' )
    def getSessionsBeforeTime(self, request):
        """Returns all sesions before the time specified for a given confernce """
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
        prof_key = ndb.Key(Profile, user_id)
        prof = prof_key.get()
        '''allow blank entries, if nothing provided return entire day'''

        if request.searchTime is None:
            request.searchTime = "23:59"
        
        """Convert given params to time objects"""
        start_time = datetime.strptime(request.searchTime, "%H:%M").time()


        """Fetch all sesion for confernce"""
        c_key = ndb.Key(urlsafe= request.websafeConferenceKey)
        sessns_base = Session.query(ancestor = c_key)


        """Find all sesions after a certain time in the day"""
        sessns = sessns_base.filter(Session.startTime <= start_time)
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        ) 


    @endpoints.method(SESS_GET_REQ_TIME, SessionForms,
        path='getSessionsAfterTime',
        http_method='GET', name = 'getSessionsAfterTime' )
    def getSessionsAfterTime(self, request):
        """Returns all sesions after the time specified for a given confernce """
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
        prof_key = ndb.Key(Profile, user_id)
        prof = prof_key.get()
        '''allow blank entries, if nothing provided return entire day'''

        if request.searchTime is None:
            request.searchTime = "23:59"
        
        """Convert given params to time objects"""
        start_time = datetime.strptime(request.searchTime, "%H:%M").time()


        """Fetch all sesion for confernce"""
        c_key = ndb.Key(urlsafe= request.websafeConferenceKey)
        sessns_base = Session.query(ancestor = c_key)


        """Find all sesions after a certain time in the day"""
        sessns = sessns_base.filter(Session.startTime >= start_time)
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        ) 

    
    #########################################
    #Task 3 Solution
    #########################################


    @endpoints.method(TASK3_SOLUTION_REQ, SessionForms,
        path='task3Solution',
        http_method='POST', name = 'task3Solution' )
    def task3Solution(self, request):
        """Returns all sesions afer after a certiain time and match the session type"""

        #Check that user is authorized and conferenc key is valid
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        try:
            prof_key = self.authUserCheck()
        except:
            raise endpoints.UnauthorizedException('User not authorized in CreateSession')
        prof = prof_key.get()


        '''allow blank entries, if nothing provided return entire day'''

        if request.searchTime is None:
            request.searchTime = "23:59"
        
        """Convert given params to time objects"""
        start_time = datetime.strptime(request.searchTime, "%H:%M").time()


        """Fetch all sesion that match the sessiontype"""
        """Cast request as a tuple to use in the IN() filter """
        query_sessionTypes = [request.sessionType, '']
        
        #All queries that aren't  whatever was provided
        sessn_type_query = Session.query(Session.typeofSession != request.sessionType)
        logging.info('Session type: %s', request.sessionType)

        #All sessions before the start time provided
        sessn_time_query = Session.query(Session.startTime <= start_time)


        #Find all sesions after a certain time in the day
        sessns = set(sessn_type_query).intersection( set(sessn_time_query) )
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessns]
        ) 


#########################################
#Task 4 Add task
#########################################
    def conferenceCreatorCheck(self, websafeConferenceKey):
        """Check to make sure current user owns the conference. 
        Returns: Conf Key"""
        try:
            conf_key = ndb.Key(urlsafe = websafeConferenceKey)
        except:
            raise endpoints.BadRequestException("Conference not found by key")

        conf_owner_key = conf_key.parent()
        if conf_owner_key != self.authUserCheck():
            raise endpoints.UnauthorizedException('You must Own the Conference to add sessions owner: %s'%conf_owner_key)
        return conf_key


    def authUserCheck(self):
        """Helper method pulls the userID for the current user, if thye aren't  authorized an 
        exception is raised
        Returns: User profile Key"""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        prof_key = ndb.Key(Profile, user_id)
        return prof_key

    @endpoints.method(message_types.VoidMessage, StringMessage,
        path='getFeaturedSpeaker',
        http_method='POST', name = 'getFeaturedSpeaker' )
    def getFeaturedSpeaker(self, request):
        """Returns the sessions a featured speaker is pressenting at."""
        return StringMessage(data=memcache.get(MEMCACHE_FEATURED_SPEAKERS_KEY) or "")
    
    @staticmethod
    def _cacheFeaturedSpeakers(websafeConferenceKey, s_key):
        '''fetch all sessions for conference'''
        wsck = ndb.Key(urlsafe = websafeConferenceKey)
        sessns = Session.query(ancestor = wsck).get()
        new_sessn = ndb.Key(urlsafe=s_key).get()
        new_speakers = new_sessn.speakers
        for speaker in new_speakers:
            sessns = Session.query(Session.speakers.IN( new_speakers ) )
            
            if sessns:
                """Add sessions speakers will be in with the Speaker at Session format"""
                cacheAnnoucement = speaker + ' is speaking at ' + ' , '.join(sessn.name for sessn in sessns)
                memcache.set(MEMCACHE_FEATURED_SPEAKERS_KEY, cacheAnnoucement)
            else:
                logging.info("Unable to set featured speakers")

       



api = endpoints.api_server([ConferenceApi]) # register API
