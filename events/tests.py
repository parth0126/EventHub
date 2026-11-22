from rest_framework import status
from rest_framework.test import APITestCase

from .models import Event, Reservation


class BaseTestCase(APITestCase):
    def setUp(self):
        self.event = Event.objects.create(
            title='PyCon India 2025',
            venue='NIMHANS Convention Centre, Bangalore',
            date='2025-09-20',
            total_seats=500,
            available_seats=500,
            status='upcoming',
        )

    def make_event(self, **kwargs):
        defaults = dict(title='Meetup', venue='Somewhere', date='2025-10-01',
                        total_seats=10, available_seats=10, status='upcoming')
        defaults.update(kwargs)
        return Event.objects.create(**defaults)

    def reserve(self, event=None, seats=2, **extra):
        payload = {
            'event': (event or self.event).pk,
            'attendee_name': 'Priya Sharma',
            'attendee_email': 'priya@example.com',
            'seats_reserved': seats,
        }
        payload.update(extra)
        return self.client.post('/api/reservations/', payload, format='json')


class EventTests(BaseTestCase):
    # 1
    def test_create_event(self):
        payload = {'title': 'DjangoCon', 'venue': 'Pune Expo', 'date': '2025-11-05',
                   'total_seats': 100, 'available_seats': 100, 'status': 'upcoming'}
        response = self.client.post('/api/events/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['title'], 'DjangoCon')
        self.assertEqual(response.data['reservations_count'], 0)
        self.assertEqual(Event.objects.count(), 2)

    # 2
    def test_list_events(self):
        self.make_event(title='Second')
        response = self.client.get('/api/events/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    # 3
    def test_filter_events_by_status(self):
        self.make_event(title='Old', status='completed')
        response = self.client.get('/api/events/?status=completed')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([e['title'] for e in response.data], ['Old'])

    # 4
    def test_filter_events_by_venue_case_insensitive_partial(self):
        self.make_event(title='Elsewhere', venue='Mumbai Hall')
        response = self.client.get('/api/events/?venue=nimhans')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([e['title'] for e in response.data], ['PyCon India 2025'])

    # 12 (create)
    def test_create_event_available_exceeds_total(self):
        payload = {'title': 'Bad', 'venue': 'X', 'date': '2025-11-05',
                   'total_seats': 10, 'available_seats': 20}
        response = self.client.post('/api/events/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['non_field_errors'],
                         ['available_seats cannot exceed total_seats.'])

    # 12 (PATCH)
    def test_patch_event_available_exceeds_total(self):
        response = self.client.patch(f'/api/events/{self.event.pk}/',
                                     {'available_seats': 501}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(f'/api/events/{self.event.pk}/',
                                     {'total_seats': 100}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_event_valid(self):
        response = self.client.patch(f'/api/events/{self.event.pk}/',
                                     {'available_seats': 450}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['available_seats'], 450)

    def test_delete_event_cascades(self):
        self.reserve()
        response = self.client.delete(f'/api/events/{self.event.pk}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Reservation.objects.count(), 0)


class ReservationTests(BaseTestCase):
    # 5
    def test_create_reservation_deducts_seats(self):
        response = self.reserve(seats=2)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'confirmed')
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 498)
        detail = self.client.get(f'/api/events/{self.event.pk}/')
        self.assertEqual(detail.data['reservations_count'], 1)

    # 6
    def test_overbooking(self):
        event = self.make_event(total_seats=1, available_seats=1)
        response = self.reserve(event=event, seats=2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'non_field_errors': ['Only 1 seat(s) available.']})
        event.refresh_from_db()
        self.assertEqual(event.available_seats, 1)
        self.assertEqual(Reservation.objects.count(), 0)

    def test_create_rechecks_availability_inside_transaction(self):
        # Simulates a race: validation passed, but another booking took the
        # seats before create() ran. The locked re-check must reject it.
        from rest_framework.exceptions import ValidationError
        from .serializers import ReservationSerializer
        event = self.make_event(total_seats=2, available_seats=2)
        serializer = ReservationSerializer(data={
            'event': event.pk, 'attendee_name': 'A', 'attendee_email': 'a@example.com',
            'seats_reserved': 2,
        })
        self.assertTrue(serializer.is_valid())
        Event.objects.filter(pk=event.pk).update(available_seats=1)
        with self.assertRaises(ValidationError) as ctx:
            serializer.save()
        self.assertEqual(ctx.exception.detail['non_field_errors'][0],
                         'Only 1 seat(s) available.')
        event.refresh_from_db()
        self.assertEqual(event.available_seats, 1)
        self.assertEqual(Reservation.objects.count(), 0)

    # 7
    def test_reserve_completed_or_cancelled_event(self):
        for event_status in ('completed', 'cancelled'):
            event = self.make_event(status=event_status)
            response = self.reserve(event=event, seats=1)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data['non_field_errors'],
                             [f'Cannot reserve seats for a {event_status} event.'])

    # 8
    def test_zero_seats(self):
        response = self.reserve(seats=0)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('seats_reserved', response.data)

    # 9
    def test_cancel_reservation_restores_seats(self):
        pk = self.reserve(seats=3).data['id']
        response = self.client.post(f'/api/reservations/{pk}/cancel/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'cancelled')
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 500)

    # 10
    def test_cancel_twice(self):
        pk = self.reserve().data['id']
        self.client.post(f'/api/reservations/{pk}/cancel/')
        response = self.client.post(f'/api/reservations/{pk}/cancel/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'error': 'Already cancelled.'})
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 500)

    def test_cancel_never_exceeds_total(self):
        pk = self.reserve(seats=5).data['id']
        Event.objects.filter(pk=self.event.pk).update(available_seats=498)
        self.client.post(f'/api/reservations/{pk}/cancel/')
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 500)

    # 11
    def test_filter_reservations_by_event_id(self):
        other = self.make_event()
        self.reserve(seats=1)
        self.reserve(event=other, seats=1)
        response = self.client.get(f'/api/reservations/?event_id={other.pk}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['event'], other.pk)

    # 13
    def test_delete_confirmed_reservation_restores_seats(self):
        pk = self.reserve(seats=4).data['id']
        response = self.client.delete(f'/api/reservations/{pk}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 500)

    def test_delete_cancelled_reservation_does_not_change_seats(self):
        pk = self.reserve(seats=4).data['id']
        self.client.post(f'/api/reservations/{pk}/cancel/')
        self.client.delete(f'/api/reservations/{pk}/')
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 500)

    def test_patch_attendee_name(self):
        pk = self.reserve().data['id']
        response = self.client.patch(f'/api/reservations/{pk}/',
                                     {'attendee_name': 'Priya S.'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['attendee_name'], 'Priya S.')

    def test_patch_cannot_change_seats_or_event(self):
        other = self.make_event()
        pk = self.reserve(seats=2).data['id']
        for change in ({'seats_reserved': 5}, {'event': other.pk}):
            response = self.client.patch(f'/api/reservations/{pk}/', change, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.event.refresh_from_db()
        self.assertEqual(self.event.available_seats, 498)


class MiddlewareTests(APITestCase):
    # 14
    def test_request_is_logged(self):
        with self.assertLogs('events.middleware', level='INFO') as logs:
            self.client.get('/api/events/')
        self.assertTrue(any('GET /api/events/ - 200 -' in line for line in logs.output))
