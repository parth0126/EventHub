from django.db import transaction
from rest_framework import serializers

from .models import Event, Reservation


class EventSerializer(serializers.ModelSerializer):
    reservations_count = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = ['id', 'title', 'venue', 'date', 'total_seats', 'available_seats',
                  'status', 'created_at', 'reservations_count']

    def get_reservations_count(self, obj):
        return obj.reservations.filter(status='confirmed').count()

    def validate(self, data):
        # On PATCH only some fields arrive, so fall back to the stored values.
        total = data.get('total_seats', getattr(self.instance, 'total_seats', 0))
        available = data.get('available_seats', getattr(self.instance, 'available_seats', 0))
        if available > total:
            raise serializers.ValidationError('available_seats cannot exceed total_seats.')
        return data


class ReservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reservation
        fields = ['id', 'event', 'attendee_name', 'attendee_email',
                  'seats_reserved', 'status', 'created_at']
        read_only_fields = ['status', 'created_at']

    def validate_seats_reserved(self, value):
        if value < 1:
            raise serializers.ValidationError('Must reserve at least 1 seat.')
        return value

    def validate(self, data):
        if self.instance is not None:
            # Updates may only touch attendee details. Changing the event or the
            # seat count would make the event's available_seats drift.
            event_changed = 'event' in data and data['event'] != self.instance.event
            seats_changed = ('seats_reserved' in data
                             and data['seats_reserved'] != self.instance.seats_reserved)
            if event_changed or seats_changed:
                raise serializers.ValidationError(
                    'event and seats_reserved cannot be changed; '
                    'cancel and create a new reservation instead.'
                )
            return data

        event = data['event']
        if event.status not in ('upcoming', 'ongoing'):
            raise serializers.ValidationError(
                f'Cannot reserve seats for a {event.status} event.'
            )
        if data['seats_reserved'] > event.available_seats:
            raise serializers.ValidationError(
                f'Only {event.available_seats} seat(s) available.'
            )
        return data

    def create(self, validated_data):
        # Seat deduction and reservation creation happen in one transaction so
        # they succeed or fail together.
        #
        # select_for_update() locks the event row until the transaction ends, so
        # a second request trying to book the same event waits, then sees the
        # updated seat count in the re-check below. NOTE: select_for_update is a
        # no-op on SQLite (it locks the whole database on write instead), but it
        # takes effect on PostgreSQL/MySQL. atomic() still makes the two writes
        # all-or-nothing on every backend.
        with transaction.atomic():
            event = Event.objects.select_for_update().get(pk=validated_data['event'].pk)
            seats = validated_data['seats_reserved']
            if seats > event.available_seats:
                raise serializers.ValidationError(
                    {'non_field_errors': [f'Only {event.available_seats} seat(s) available.']}
                )
            event.available_seats -= seats
            event.save()
            validated_data['event'] = event
            return Reservation.objects.create(**validated_data)
