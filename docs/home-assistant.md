# Home Assistant

There are two ways to connect ZePrint to Home Assistant. Both can run at the
same time.

## 1. MQTT discovery (recommended)

Requires HA's MQTT integration and a broker (e.g. the Mosquitto add-on). Add
this to ZePrint's environment:

```yaml
MQTT_HOST: 192.168.1.10        # your broker
MQTT_USERNAME: zeprint         # if the broker needs auth
MQTT_PASSWORD: secret
ZEPRINT_URL: http://192.168.1.20:3231   # optional: "Visit device" link in HA
```

HA then shows a **ZePrint** device with:

| Entity | |
|---|---|
| `button.zeprint_print_<label>` | One per label. Prints with the label's **saved defaults** (set them in the web UI with *Save as defaults*) on the default printer. |
| `sensor.zeprint_last_print_job` | `queued` / `rendering` / `sending` / `done` / `error`. The whole job is in its attributes (label, printer, error…). |
| `sensor.zeprint_<printer>_status` | `ready`, `printing`, `paper_out`, `head_open`, `paused`, `offline`… from `~HS`, polled every `MQTT_STATUS_INTERVAL` seconds (60). Attributes hold the details and the model/firmware. |

Entity IDs follow HA's naming for the device ("ZePrint") and the entity names
above. Check *Settings → Devices → ZePrint* for the exact IDs.

### Printing with parameters from an automation

Publish to `zeprint/print/<label>`. The payload is empty (defaults) or a JSON
object of label parameters, plus optional `printer`, `size` and `copies`:

```yaml
automation:
  - alias: Morning tide card
    triggers:
      - trigger: time
        at: "06:30:00"
    actions:
      - action: mqtt.publish
        data:
          topic: zeprint/print/tides
          payload: '{"station": "9447130", "size": "2x1"}'

  - alias: Weather label when the sailing calendar has an event today
    triggers:
      - trigger: calendar
        event: start
        entity_id: calendar.sailing
        offset: "-2:00:00"
    actions:
      - action: mqtt.publish
        data:
          topic: zeprint/print/weather
          payload: '{"hours_ahead": 12}'

  - alias: Return address labels on demand (2x1)
    triggers:
      - trigger: state
        entity_id: input_button.return_address_labels
    actions:
      - action: mqtt.publish
        data:
          topic: zeprint/print/address
          payload: '{"include": "from", "size": "2x1", "copies": 10}'

  - alias: Print a shipping label from a URL
    # e.g. fired by a script that receives a label link from your shop
    triggers:
      - trigger: event
        event_type: shipping_label_ready
    actions:
      - action: mqtt.publish
        data:
          topic: zeprint/print/image
          payload: '{"image": "{{ trigger.event.data.url }}", "pages": "all"}'

  - alias: Tell me when the printer runs out of labels
    triggers:
      - trigger: state
        entity_id: sensor.zeprint_zebra_gx430t_status
        to: paper_out
    actions:
      - action: notify.mobile_app_phone
        data:
          message: "The Zebra is out of labels."
```

Other topics (the base topic is `MQTT_BASE_TOPIC`, default `zeprint`):

| Topic | Direction | |
|---|---|---|
| `zeprint/print` | → | JSON with `"label"` plus parameters (one topic for everything). |
| `zeprint/raw/<printer>` | → | Raw ZPL, sent as-is. |
| `zeprint/status` | ← | `online` / `offline` (retained, last will). |
| `zeprint/job` | ← | Every job state change (retained). |
| `zeprint/printer/<id>` | ← | Printer status (retained). |
| `zeprint/error` | ← | Rejected commands, e.g. bad parameters. |

## 2. REST (`rest_command`)

This needs no broker. In `configuration.yaml`:

```yaml
rest_command:
  zeprint_print:
    url: "http://zeprint.local:3231/api/labels/{{ label }}/print"
    method: POST
    content_type: application/json
    # headers:
    #   Authorization: "Bearer !secret zeprint_token"   # if ZEPRINT_API_TOKEN is set
    payload: "{{ params | default({}) | to_json }}"
```

```yaml
actions:
  - action: rest_command.zeprint_print
    data:
      label: satellite
      params: {norad: "25544", size: "4x6"}
```

`rest_command` responses include the job, so a script can check
`response.content.status`.

## Label previews on a dashboard

`GET /api/labels/<label>/preview.png?size=2x1&<param>=<value>` renders the
label as an image without printing it. You can show it with a Generic Camera
or an Image entity:

```yaml
# Settings → Devices & services → Add → Generic Camera
still_image_url: http://zeprint.local:3231/api/labels/tides/preview.png?size=2x1
```

(With `ZEPRINT_API_TOKEN` set, add `&token=…`.)

## What's next

This is groundwork. The same core, reached through `ZePrint.events` and the
service API, can support a HACS custom integration (a `notify`-style print
action, config flow) or an HA add-on. It could also support scheduled prints
inside ZePrint itself. New labels added as plugins show up as buttons
automatically.
