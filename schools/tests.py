from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from schedule.models import ClassGroup, Subject
from teachers.models import Teacher

from .middleware import resolve_school
from .models import School, get_default_school


def make_school(subdomain, name="Other school") -> School:
    return School.objects.create(name=name, subdomain=subdomain)


def make_teacher(school, username, role=Teacher.Role.MEMBER) -> Teacher:
    user = User.objects.create_user(username=username)
    return Teacher.objects.create(user=user, school=school, grade_level=Teacher.GradeLevel.PRIMARY, role=role)


@override_settings(BASE_DOMAIN="doplent.example.com")
class ResolveSchoolTests(TestCase):
    def setUp(self):
        self.root = get_default_school()
        self.other = make_school("escola2")

    def test_bare_base_domain_resolves_to_the_root_school(self):
        self.assertEqual(resolve_school("doplent.example.com"), self.root)

    def test_subdomain_resolves_to_its_school(self):
        self.assertEqual(resolve_school("escola2.doplent.example.com"), self.other)

    def test_www_prefixed_base_domain_resolves_to_the_root_school(self):
        # DJANGO_ALLOWED_HOSTS in production accepts both the bare domain and
        # a "www." one - "www" must not be mistaken for a school subdomain.
        self.assertEqual(resolve_school("www.doplent.example.com"), self.root)

    def test_unknown_subdomain_resolves_to_none(self):
        self.assertIsNone(resolve_school("ghost.doplent.example.com"))

    def test_unrelated_host_resolves_to_none(self):
        self.assertIsNone(resolve_school("evil.com"))

    def test_testserver_resolves_to_the_root_school(self):
        # Django's test client sends this Host unless overridden - see
        # resolve_school's docstring for why this alias exists.
        self.assertEqual(resolve_school("testserver"), self.root)

    @override_settings(DEBUG=True)
    def test_localhost_and_127_0_0_1_resolve_to_the_root_school_in_debug(self):
        # So `manage.py runserver` works against http://127.0.0.1:8000/ or
        # http://localhost:8000/ without setting DJANGO_BASE_DOMAIN locally.
        self.assertEqual(resolve_school("localhost"), self.root)
        self.assertEqual(resolve_school("127.0.0.1"), self.root)

    @override_settings(DEBUG=False)
    def test_localhost_and_127_0_0_1_do_not_resolve_outside_debug(self):
        # Must not become an accidental backdoor in production.
        self.assertIsNone(resolve_school("localhost"))
        self.assertIsNone(resolve_school("127.0.0.1"))


class CrossSchoolIsolationTests(TestCase):
    """A teacher from one school must never see or affect another school's
    data, even once both are reachable from the same deployment."""

    def setUp(self):
        self.school_a = get_default_school()
        self.school_b = make_school("escolab", name="Escola B")
        self.staff_a = make_teacher(self.school_a, "staff_a", role=Teacher.Role.STAFF)
        self.staff_b = make_teacher(self.school_b, "staff_b", role=Teacher.Role.STAFF)
        self.group_b = ClassGroup.objects.create(name="Grup B", grade_level=Teacher.GradeLevel.PRIMARY, school=self.school_b)

    def test_staff_of_school_a_cannot_open_school_bs_group_schedule(self):
        self.client.force_login(self.staff_a.user)
        response = self.client.get(reverse("group_schedule", args=[self.group_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_group_list_only_shows_the_current_schools_groups(self):
        ClassGroup.objects.create(name="Grup A", grade_level=Teacher.GradeLevel.PRIMARY, school=self.school_a)
        self.client.force_login(self.staff_a.user)
        response = self.client.get(reverse("group_list"))
        self.assertContains(response, "Grup A")
        self.assertNotContains(response, "Grup B")

    def test_teacher_from_a_different_school_gets_403_on_dashboard(self):
        # A logged-in teacher whose own school doesn't match request.school
        # (the root school, since no Host header override was given here).
        self.client.force_login(self.staff_b.user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_same_subject_name_allowed_in_two_different_schools(self):
        Subject.objects.create(name="Matemàtiques", school=self.school_a)
        # Must not raise IntegrityError - uniqueness is per-school now.
        Subject.objects.create(name="Matemàtiques", school=self.school_b)
        self.assertEqual(Subject.objects.filter(name="Matemàtiques").count(), 2)
