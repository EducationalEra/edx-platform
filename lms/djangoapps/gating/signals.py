"""
Signal handlers for the gating djangoapp
"""
from django.dispatch import receiver

from gating import api as gating_api
from lms.djangoapps.grades.signals.signals import PROBLEM_WEIGHTED_SCORE_CHANGED
from opaque_keys.edx.keys import CourseKey, UsageKey
from xmodule.modulestore.django import modulestore


@receiver(PROBLEM_WEIGHTED_SCORE_CHANGED)
def handle_score_changed(**kwargs):
    """
    Receives the PROBLEM_WEIGHTED_SCORE_CHANGED signal sent by LMS when a student's score has changed
    for a given component and triggers the evaluation of any milestone relationships
    which are attached to the updated content.

    Arguments:
        kwargs (dict): Contains user ID, course key, and content usage key

    Returns:
        None
    """
    course_id = type(kwargs.get('course_id')) in (str, unicode) and CourseKey.from_string(kwargs.get('course_id')) or kwargs.get('course_id')
    usage_id = type(kwargs.get('usage_id')) in (str, unicode) and UsageKey.from_string(kwargs.get('usage_id')) or kwargs.get('usage_id')
    course = modulestore().get_course(course_id)
    block = modulestore().get_item(usage_id)
    gating_api.evaluate_prerequisite(course, block, kwargs.get('user_id'))
    gating_api.evaluate_entrance_exam(course, block, kwargs.get('user_id'))
