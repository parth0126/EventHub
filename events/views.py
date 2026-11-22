from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Event, Reservation
from .serializers import EventSerializer, ReservationSerializer


def _restore_seats(event_pk, seats):
    """Give seats back to an event without ever exceeding total_seats.
    Must be called inside transaction.atomic()."""
    event = Event.objects.select_for_update().get(pk=event_pk)
    event.available_seats = min(event.available_seats + seats, event.total_seats)
    event.save()
    return event


class EventViewSet(viewsets.ModelViewSet):
    serializer_class = EventSerializer

    def get_queryset(self):
        queryset = Event.objects.all()
        status = self.request.query_params.get('status')
        venue = self.request.query_params.get('venue')
        if status:
            queryset = queryset.filter(status=status)
        if venue:
            queryset = queryset.filter(venue__icontains=venue)
        return queryset


class ReservationViewSet(viewsets.ModelViewSet):
    serializer_class = ReservationSerializer

    def get_queryset(self):
        queryset = Reservation.objects.select_related('event').all()
        event_id = self.request.query_params.get('event_id')
        if event_id:
            queryset = queryset.filter(event_id=event_id)
        return queryset

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        reservation = self.get_object()
        with transaction.atomic():
            # Lock and re-read the reservation so two simultaneous cancels
            # can't both restore seats.
            reservation = Reservation.objects.select_for_update().get(pk=reservation.pk)
            if reservation.status == 'cancelled':
                return Response({'error': 'Already cancelled.'}, status=400)
            reservation.event = _restore_seats(reservation.event_id, reservation.seats_reserved)
            reservation.status = 'cancelled'
            reservation.save()
        return Response(self.get_serializer(reservation).data)

    def perform_destroy(self, instance):
        with transaction.atomic():
            reservation = Reservation.objects.select_for_update().get(pk=instance.pk)
            if reservation.status == 'confirmed':
                _restore_seats(reservation.event_id, reservation.seats_reserved)
            reservation.delete()
