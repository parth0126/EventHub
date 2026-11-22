# EventHub

EventHub is a REST API for event ticketing, built with Django REST Framework. You can create events, reserve seats, and cancel reservations. The API tracks available seats on every booking, cancellation and deletion, and it rejects overbooking.

## Tech stack

- Python 3.12+ (required by Django 6.x, which `requirements.txt` pins)
- Django 6.1
- Django REST Framework 3.18
- SQLite (the default Django database)

## How to run

```bash
# 1. Clone
git clone https://github.com/<your-username>/eventhub.git
cd eventhub

# 2. Create and activate a virtual environment
python -m venv venv
# Windows (PowerShell)
venv\Scripts\Activate.ps1
# Windows (cmd)
venv\Scripts\activate.bat
# macOS / Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create the database tables
python manage.py migrate

# 5. Start the server (API at http://127.0.0.1:8000/api/)
python manage.py runserver

# 6. Run the tests
python manage.py test
```

Each request is logged to the terminal by the request-logging middleware, for example:

```
[2025-09-01 10:15:02,113] INFO events.middleware: POST /api/reservations/ - 201 - 0.08s
```

## Endpoints

| Method | URL | Description |
|---|---|---|
| GET | `/api/events/` | List all events. Filter with `?status=upcoming` (exact) or `?venue=bangalore` (case-insensitive, partial match). |
| POST | `/api/events/` | Create an event. `available_seats` must not exceed `total_seats`. |
| GET | `/api/events/{id}/` | Retrieve one event, including `reservations_count` (confirmed reservations). |
| PUT/PATCH | `/api/events/{id}/` | Update an event. PATCH is checked against the stored values for any seat field you leave out. |
| DELETE | `/api/events/{id}/` | Delete an event and all of its reservations (cascade). |
| GET | `/api/reservations/` | List reservations, newest first. Filter with `?event_id=1`. |
| POST | `/api/reservations/` | Create a reservation and deduct the seats from the event. |
| GET | `/api/reservations/{id}/` | Retrieve one reservation. |
| PUT/PATCH | `/api/reservations/{id}/` | Update attendee name/email only. You can't change `event` or `seats_reserved`. |
| DELETE | `/api/reservations/{id}/` | Delete a reservation. If it was confirmed, its seats go back to the event. |
| POST | `/api/reservations/{id}/cancel/` | Cancel a reservation and give its seats back to the event. |

## Sample requests and responses

### Create an event

`POST /api/events/`

```json
{
  "title": "PyCon India 2025",
  "venue": "NIMHANS Convention Centre, Bangalore",
  "date": "2025-09-20",
  "total_seats": 500,
  "available_seats": 500,
  "status": "upcoming"
}
```

**201 Created**

```json
{
  "id": 1,
  "title": "PyCon India 2025",
  "venue": "NIMHANS Convention Centre, Bangalore",
  "date": "2025-09-20",
  "total_seats": 500,
  "available_seats": 500,
  "status": "upcoming",
  "created_at": "2025-09-01T10:14:51.293863Z",
  "reservations_count": 0
}
```

### Reserve seats

`POST /api/reservations/`

```json
{
  "event": 1,
  "attendee_name": "Priya Sharma",
  "attendee_email": "priya@example.com",
  "seats_reserved": 2
}
```

**201 Created** (the event now has `available_seats: 498`)

```json
{
  "id": 1,
  "event": 1,
  "attendee_name": "Priya Sharma",
  "attendee_email": "priya@example.com",
  "seats_reserved": 2,
  "status": "confirmed",
  "created_at": "2025-09-01T10:15:02.588889Z"
}
```

### Overbooking

`POST /api/reservations/`

```json
{
  "event": 1,
  "attendee_name": "Rahul Verma",
  "attendee_email": "rahul@example.com",
  "seats_reserved": 600
}
```

**400 Bad Request**

```json
{
  "non_field_errors": ["Only 498 seat(s) available."]
}
```

Other validation errors:

- Booking a completed or cancelled event: `{"non_field_errors": ["Cannot reserve seats for a completed event."]}`
- `seats_reserved: 0`: `{"seats_reserved": ["Must reserve at least 1 seat."]}`

### Cancel a reservation

`POST /api/reservations/1/cancel/`

**200 OK** (the event goes back to `available_seats: 500`)

```json
{
  "id": 1,
  "event": 1,
  "attendee_name": "Priya Sharma",
  "attendee_email": "priya@example.com",
  "seats_reserved": 2,
  "status": "cancelled",
  "created_at": "2025-09-01T10:15:02.588889Z"
}
```

Cancelling again returns **400** `{"error": "Already cancelled."}`.

## Design Decision

**Seat deduction and reservation creation run inside one `transaction.atomic()` block. The event row is locked with `select_for_update()`, and availability is checked again inside the transaction.**

Booking makes two writes: it lowers `event.available_seats` and it inserts a `Reservation` row. That causes two problems:

1. **Race condition.** The serializer's `validate()` checks availability before `create()` runs. Suppose two users try to book the last seat at the same moment. Both requests can pass validation before either has saved, and the event ends up overbooked. To prevent this, `create()` re-fetches the event with `Event.objects.select_for_update().get(pk=...)`, which locks the row. The second request waits until the first one commits. It then re-reads the updated seat count, fails the re-check, and gets the same `"Only X seat(s) available."` error.
2. **Partial writes.** If the seat deduction succeeds but the reservation insert fails, seats disappear with no reservation to show for them. Inside `atomic()`, both writes succeed or neither does.

**SQLite vs PostgreSQL caveat:** `select_for_update()` does nothing on SQLite. SQLite has no row-level locks and instead locks the whole database for each write. On PostgreSQL or MySQL the row lock takes effect. `atomic()` still makes the two writes all-or-nothing on every backend. Use PostgreSQL in production to get the full protection against races.

Cancel and DELETE use the same pattern. They lock the reservation and the event inside `atomic()`, so two cancel requests at the same moment can't both give the seats back.

### Other decisions

- **A reservation's event and seat count are fixed once it is created.** PATCH/PUT can only change `attendee_name` and `attendee_email`. If a request sends a different `event` or `seats_reserved`, it gets a 400 error that says to cancel and create a new reservation instead. This keeps the event's seat count accurate.
- **DELETE gives seats back.** Deleting a *confirmed* reservation returns its seats to the event. Deleting an already-*cancelled* reservation changes nothing, because its seats were returned when it was cancelled.
- **Seats never exceed the total.** When seats are returned, `available_seats` is capped with `min(available + seats, total_seats)`.
- **PATCH on events uses the stored values for missing fields.** Sending only `available_seats` (or only `total_seats`) is still checked against the other stored value.

## Screenshots

### Successful reservation (201)

![Successful reservation](screenshots/reservation_201.png)

### Overbooking (400)

![Overbooking](screenshots/overbooking_400.png)

### Successful cancellation

![Successful cancellation](screenshots/cancel_200.png)
# EventHub
