import logging
import random
import string
import urllib
import uuid

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.generic import View
from eventtracking import tracker
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.credit.models import CreditCourse, CreditRequest, CreditProvider
from openedx.core.djangoapps.user_api.accounts.api import check_account_exists
from openedx.core.djangoapps.user_api.preferences.api import update_email_opt_in
from openedx.core.lib.api.authentication import OAuth2AuthenticationAllowInactiveUser
from openedx.core.lib.api.permissions import ApiKeyHeaderPermission
from openedx.core.lib.exceptions import CourseNotFoundError
from openedx.core.lib.log_utils import audit_log
from openedx.features.course_experience import course_home_url_name
from openedx.features.enterprise_support.api import EnterpriseApiClient, EnterpriseApiException, enterprise_enabled
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST, HTTP_406_NOT_ACCEPTABLE
from rest_framework.views import APIView
from social_django.models import UserSocialAuth

from edxmako.shortcuts import render_to_response
from enrollment import api
from enrollment.errors import CourseEnrollmentError, CourseEnrollmentExistsError, CourseModeNotFoundError
from enrollment.views import REQUIRED_ATTRIBUTES
from ospp_api.mixins import EligibleCheckViewMixin, MethodViewWithMakoMixin
from ospp_api.models import OSPPEnrollmentFeature
from ospp_api.utils import get_learner_info, apply_user_status_to_enroll
from student.models import CourseEnrollment, User, CourseAccessRole
from student.views import create_account_with_params
from third_party_auth.models import SAMLProviderConfig

log = logging.getLogger(__name__)

REQUIRED_CREATE_USER_PARAMS = ('username', 'email', 'name_id')


class CreateUserView(APIView):
    authentication_classes = OAuth2AuthenticationAllowInactiveUser,
    permission_classes = ApiKeyHeaderPermission,

    USER_ALREADY_EXIST_ERROR = 1

    class ValidationException(Exception):
        """
        Custom exception, that contains attribute `error_response` with HTTP response that represents validation error.
        """

        def __init__(self, error_response):
            super(CreateUserView.ValidationException, self).__init__(error_response)
            self.error_response = error_response

    def update_user_data_with_default_params(self, data):
        data['honor_code'] = "True"
        data['terms_of_service'] = "True"

        # Generate fake password and set name equal to the username
        data['password'] = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
        if 'first_name' not in data:
            data['first_name'] = ''

        if 'last_name' not in data:
            data['last_name'] = ''

        data['name'] = '{} {}'.format(data['first_name'], data['last_name']).strip() or data['username']

        # Avoid sending activation email
        data['send_activation_email'] = False

    def missed_params_validation(self, data):
        """
        Check that requirement field exist.
        Return None if data valid and error response when some data missed.
        """
        missed_params = [param for param in REQUIRED_CREATE_USER_PARAMS if not data.get(param)]
        if missed_params:
            error_msg = "Required parameter(s): {} were not provided".format(" ,".join(missed_params))
            raise self.ValidationException(Response({"user_message": error_msg}, status=400))
        return

    def try_get_simple_user(self, data):
        email = data['email']
        username = data['username']
        if check_account_exists(email=email, username=username):
            user = User.objects.filter(Q(email=email) | Q(username=username)).first()
            if not UserSocialAuth.objects.filter(user=user).exists():
                return user
            errors = {
                "user_message": "User already exists",
                "error_code": CreateUserView.USER_ALREADY_EXIST_ERROR,
                "user_id": user.id,
            }
            raise self.ValidationException(Response(errors, status=409))
        return

    def create_new_user(self, request, data):
        user = create_account_with_params(request, data)
        user.is_active = True
        user.first_name = data['first_name']
        user.last_name = data['last_name']
        user.save()
        return user

    def make_user_social(self, user, data):
        idp_name = SAMLProviderConfig.objects.first().backend_name
        social_user_id = '{}:{}'.format(idp_name, data.pop('name_id'))
        UserSocialAuth.objects.create(user=user, provider=idp_name, uid=social_user_id)

    def post(self, request):
        """
        Creates a new user account

        URL: /ospp_api/v0/create_user/
        Arguments:
            request (HttpRequest)
                HEAD
                {
                    "x-edx-api-key": "EDX-API-TOKEN"
                }
                JSON (application/json)
                {
                    "username": "user4",
                    "email": "userUdot@example.com",
                    "name_id": "auth0|5a1827996asd85k0cb994082" # auth0 user's profile ID
                    "first_name": "Test" # Optional parameter
                    "last_name": "User" # Optional parameter
                }
        Returns:
            HttpResponse: 200 on success, {"user_id": 3}
            HttpResponse: 400 if the request is not valid
            HttpResponse: 409 if an account with the given username or email address already exists
        """
        data = request.data
        self.update_user_data_with_default_params(data)

        try:
            self.missed_params_validation(data)
            user = self.try_get_simple_user(data)
        except self.ValidationException as e:
            return e.error_response

        try:
            user = user or self.create_new_user(request, data)
            self.make_user_social(user, data)
        except ValidationError:
            errors = {"user_message": "Wrong parameters on user creation"}
            return Response(errors, status=400)
        except AttributeError:
            errors = {"user_message": "Wrong Identity Provider's configuration"}
            return Response(errors, status=400)

        return Response({'user_id': user.id}, status=200)


class EnrollUserView(APIView):
    authentication_classes = OAuth2AuthenticationAllowInactiveUser,
    permission_classes = ApiKeyHeaderPermission,

    def post(self, request):
        """
        Create/Update enrollment

        URL: /ospp_api/v0/enrollments
        Arguments:
            request (HttpRequest)
                HEAD
                {
                    "x-edx-api-key": "EDX-API-TOKEN"
                }
                JSON (application/json)
                {
                  "user_id": "16",
                  "eligibility_status": ture,
                  "partner_logo": "https://pbs.twimg.com/profile_images/596777148435705856/tsE4inUQ.jpg"
                  "is_active": "default:true, [true|false]",
                  "mode":"default:audit, [honor|professional|verified|audit]",
                  "course_details": {
                    "course_id": "course-v1:Rom+RM1+2015"
                  }
                }

        Returns:
            HttpResponse: 200 on success,
                {
                  "created": "2017-12-01T15:32:14.767504Z",
                  "mode": "honor",
                  "is_active": true,
                  "course_details": {
                    "course_id": "course-v1:Rom+RM1+2015",
                    "course_name": "Tor",
                    "enrollment_start": null,
                    "enrollment_end": null,
                    "course_start": "2015-01-01T00:00:00Z",
                    "course_end": null,
                    "invite_only": false,
                    "course_modes": [
                      {
                        "slug": "honor",
                        "name": "Honor Certificate",
                        "min_price": 0,
                        "suggested_prices": "",
                        "currency": "usd",
                        "expiration_datetime": null,
                        "description": null,
                        "sku": "8812E4C",
                        "bulk_sku": null
                      },
                      {
                        "slug": "verified",
                        "name": "Verified Certificate",
                        "min_price": 100,
                        "suggested_prices": "",
                        "currency": "usd",
                        "expiration_datetime": "2018-01-10T00:00:00Z",
                        "description": null,
                        "sku": "4CDEA2A",
                        "bulk_sku": null
                      }
                    ]
                  },
                  "user": "Honor12"
                }
            HttpResponse: 400 if the request is not valid
            HttpResponse: 406 if an account with the given user id not found

        """
        course_id = urllib.unquote(request.data.get('course_details', {}).get('course_id')).replace(' ', '+')
        if not course_id:
            return Response(
                    status=HTTP_400_BAD_REQUEST,
                    data={"message": u"Course ID must be specified to create a new enrollment."}
            )

        try:
            course_id = CourseKey.from_string(course_id)
        except InvalidKeyError:
            return Response(
                    status=HTTP_400_BAD_REQUEST,
                    data={
                        "message": u"No course '{course_id}' found for enrollment".format(course_id=course_id)
                    }
            )

        user_id = request.data.get('user_id')

        if not user_id:
            return Response(
                    status=HTTP_400_BAD_REQUEST,
                    data={"message": u"User ID must be specified to create a new enrollment."}
            )

        try:
            user = User.objects.get(id=user_id)
        except ObjectDoesNotExist:
            return Response(
                    status=HTTP_406_NOT_ACCEPTABLE,
                    data={
                        'message': u'The user with id {} does not exist.'.format(user_id)
                    }
            )
        username = user.username

        mode = request.data.get('mode')

        partner_logo = request.data.get('partner_logo', '')
        eligibility_status = request.data.get('eligibility_status', False)

        try:
            is_active = request.data.get('is_active')
            # Check if the requested activation status is None or a Boolean
            if is_active is not None and not isinstance(is_active, bool):
                return Response(
                        status=HTTP_400_BAD_REQUEST,
                        data={
                            'message': (u"'{value}' is an invalid enrollment activation status.").format(
                                    value=is_active)
                        }
                )

            enterprise_course_consent = request.data.get('enterprise_course_consent')
            # Check if the enterprise_course_enrollment is a boolean
            if enterprise_enabled() and enterprise_course_consent is not None:
                if not isinstance(enterprise_course_consent, bool):
                    return Response(
                            status=HTTP_400_BAD_REQUEST,
                            data={
                                'message': (u"'{value}' is an invalid enterprise course consent value.").format(
                                        value=enterprise_course_consent
                                )
                            }
                    )
                try:
                    EnterpriseApiClient().post_enterprise_course_enrollment(
                            username,
                            unicode(course_id),
                            enterprise_course_consent
                    )
                except EnterpriseApiException as error:
                    log.exception("An unexpected error occurred while creating the new EnterpriseCourseEnrollment "
                                  "for user [%s] in course run [%s]", username, course_id)
                    raise CourseEnrollmentError(error.message)

            enrollment_attributes = request.data.get('enrollment_attributes')
            enrollment = api.get_enrollment(username, unicode(course_id))
            mode_changed = enrollment and mode is not None and enrollment['mode'] != mode
            active_changed = enrollment and is_active is not None and enrollment['is_active'] != is_active
            missing_attrs = []
            if enrollment_attributes:
                actual_attrs = [
                    u"{namespace}:{name}".format(**attr)
                    for attr in enrollment_attributes
                ]
                missing_attrs = set(REQUIRED_ATTRIBUTES.get(mode, [])) - set(actual_attrs)
            if mode_changed or active_changed:
                if mode_changed and active_changed and not is_active:
                    # if the requester wanted to deactivate but specified the wrong mode, fail
                    # the request (on the assumption that the requester had outdated information
                    # about the currently active enrollment).
                    msg = u"Enrollment mode mismatch: active mode={}, requested mode={}. Won't deactivate.".format(
                            enrollment["mode"], mode
                    )
                    log.warning(msg)
                    return Response(status=HTTP_400_BAD_REQUEST, data={"message": msg})

                if len(missing_attrs) > 0:
                    msg = u"Missing enrollment attributes: requested mode={} required attributes={}".format(
                            mode, REQUIRED_ATTRIBUTES.get(mode)
                    )
                    log.warning(msg)
                    return Response(status=HTTP_400_BAD_REQUEST, data={"message": msg})

                response = api.update_enrollment(
                        username,
                        unicode(course_id),
                        mode=mode,
                        is_active=is_active,
                        enrollment_attributes=enrollment_attributes
                )
            else:
                # Will reactivate inactive enrollments.
                response = api.add_enrollment(
                        username,
                        unicode(course_id),
                        mode=mode,
                        is_active=is_active,
                        enrollment_attributes=enrollment_attributes
                )

            email_opt_in = request.data.get('email_opt_in', None)
            if email_opt_in is not None:
                org = course_id.org
                update_email_opt_in(request.user, org, email_opt_in)

            log.info('The user [%s] has already been enrolled in course run [%s].', username, course_id)
            return Response(response)
        except CourseModeNotFoundError as error:
            return Response(
                    status=HTTP_400_BAD_REQUEST,
                    data={
                        "message": (
                            u"The [{mode}] course mode is expired "
                            u"or otherwise unavailable for course run [{course_id}]."
                        ).format(mode=mode, course_id=course_id),
                        "course_details": error.data
                    })
        except CourseNotFoundError:
            return Response(
                    status=HTTP_400_BAD_REQUEST,
                    data={
                        "message": u"No course '{course_id}' found for enrollment".format(course_id=course_id)
                    }
            )
        except CourseEnrollmentExistsError as error:
            log.warning('An enrollment already exists for user [%s] in course run [%s].', username, course_id)
            return Response(data=error.enrollment)
        except CourseEnrollmentError:
            log.exception("An error occurred while creating the new course enrollment for user "
                          "[%s] in course run [%s]", username, course_id)
            return Response(
                    status=HTTP_400_BAD_REQUEST,
                    data={
                        "message": (
                            u"An error occurred while creating the new course enrollment for user "
                            u"'{username}' in course '{course_id}'"
                        ).format(username=username, course_id=course_id)
                    }
            )
        finally:
            current_enrollment = api.get_enrollment(username, unicode(course_id))
            enrollment = CourseEnrollment.objects.get(course_id=course_id, user_id=user.id)  # type: CourseEnrollment
            if not OSPPEnrollmentFeature.objects.filter(enrollment_id=enrollment.id).exists():
                OSPPEnrollmentFeature(
                        enrollment_id=enrollment.id,
                        partner_logo=partner_logo,
                        eligibility_status=eligibility_status
                ).save()
            else:
                enrollment.ospp_feature.partner_logo = partner_logo
                enrollment.ospp_feature.eligibility_status = eligibility_status
                enrollment.ospp_feature.save()
            audit_log(
                    'enrollment_change_requested',
                    course_id=unicode(course_id),
                    requested_mode=mode,
                    actual_mode=current_enrollment['mode'] if current_enrollment else None,
                    requested_activation=is_active,
                    actual_activation=current_enrollment['is_active'] if current_enrollment else None,
                    user_id=user.id
            )


class SendCreditRequest(APIView):
    def post(self, request):
        course_id = request.data['course_id']
        course_key = CourseKey.from_string(course_id)

        if not CourseEnrollment.objects.filter(user__id=request.user.id, course_id=course_key, mode='credit').exists():
            return Response({'status': 'wrongg enrollment type'}, status=400)
        course = CreditCourse.objects.filter(course_key=course_key).first()
        username = request.user.username
        if not CreditRequest.objects.filter(username=username, course=course).exists():
            CreditRequest.objects.create(
                username=username,
                course=course,
                provider=CreditProvider.objects.first(),  # NOTE valid only system with single provider.
                uuid=uuid.uuid4().hex,
                parameters={'description': 'autogenerated'},
                status=CreditRequest.REQUEST_STATUS_PENDING,
            )
            eventtracking = tracker.get_tracker()
            context = {
                'username': self.request.user.username,
                'course_id': course_id
            }
            with eventtracking.context('custom_user_context', context):
                eventtracking.emit('credit.request.created', {})

        return Response({'status': 'ok'}, status=200)


def ospp_registration_stub(request):
    return render_to_response('ospp/blank_registration.html', {})


class OsppDashboardView(MethodViewWithMakoMixin, EligibleCheckViewMixin, View):
    """
    Overwrite original dashboard view.

    This view - wrapper over the original dashboard view (student.views.dashboard).
    It sends studio_access parameter to the rendered template.
    """

    def view_module(self):
        from student import views
        return views

    def update_context(self, request, context):
        context['studio_access'] = CourseAccessRole.objects.filter(
            user__id=request.user.id,
            role__in=['instructor', 'staff']
        ).exists()
        context['benefit_type'] = self.benefit_type
        return context

    def get(self, request):
        # Called method dashboard from the patched module (represents original student`s dashboard view with the
        # updated context)
        return self.get_patched_module(request).dashboard(request)


class RoutView(EligibleCheckViewMixin, View):
    @staticmethod
    def course_key_from_request(request):
        return request.GET['course_id'].replace(' ', '+')

    def _rout_to_course(self, request):
        course_id = self.course_key_from_request(request)
        course_key = CourseKey.from_string(course_id)
        redirect_url = reverse(course_home_url_name(course_key), args=[course_id])
        return redirect(redirect_url)

    def _rout_to_credit(self, request):
        credit_redirect = '{root}/credit/checkout/{course_id}/'.format(
                root=settings.ECOMMERCE_PUBLIC_URL_ROOT,
                course_id=self.course_key_from_request(request)
        )
        return redirect(credit_redirect)

    def _rout_to_verify(self, request):
        course_id = self.course_key_from_request(request)
        redirect_url = reverse('ospp_api_v0:enrollments_status_update', kwargs={'course_id': course_id})
        return redirect(redirect_url)

    def get(self, request, lms_page_name):
        return (
                {
                    'view': self._rout_to_course,
                    'audit': self._rout_to_course,
                    'get_credit': self._rout_to_credit,
                    'pursue_credit': self._rout_to_verify,
                }.get(lms_page_name, None) or (lambda _: redirect('/')))(request)


class UpdateEnrollForCourseView(View):

    def get(self, request, course_id):
        if request.user.is_authenticated():
            enrollment = CourseEnrollment.objects.filter(course_id=CourseKey.from_string(course_id), user=request.user)
            if enrollment.exists():
                student_state = get_learner_info(request.user.id)
                if student_state:
                    apply_user_status_to_enroll(request.user, enrollment.first(), student_state, True)
        return redirect(reverse('verify_student_upgrade_and_verify', kwargs={'course_id': course_id}))
