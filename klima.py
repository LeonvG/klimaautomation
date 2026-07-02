# klima.py — Selbstregelnde Klimasteuerung, Variante A (Inverter-Strategie)
#
# Ersetzt die vier YAML-Automationen:
#   Klima – Hauptsteuerung, Klima – Luefter Tag/Nacht,
#   Klima – Manuelle Pause, Klima – Tuer Haus Kuehlen
#
# Ablage: <ha-config>/pyscript/klima.py
# Benoetigte Helfer (bleiben bestehen): input_boolean.klima_automatik,
#   input_number.klima_zieltemperatur, timer.klima_automatik_pause,
#   script.klimahauskuhlen
#
# WICHTIG: Nie gleichzeitig mit klima_onoff.py (Variante B) im pyscript-Ordner
# ablegen — beide steuern dieselbe Anlage.

from datetime import datetime, time as dtime

# =====================  Konfiguration  =====================

KLIMA         = "climate.klimaanlage"
SENSOR_INNEN  = "sensor.timmerflotte_temp_hmd_sensor_temperature"
SENSOR_AUSSEN = "sensor.klimaanlage_outdoor"
ZIEL_HELFER   = "input_number.klima_zieltemperatur"
PERSONEN      = ["person.leon", "person.tina"]   # anwesend = mindestens eine Person "home"
FENSTER       = ["binary_sensor.fensterlinks_opening",
                 "binary_sensor.fensterrechts_opening"]
TUER          = "binary_sensor.tur_opening"
AUTOMATIK     = "input_boolean.klima_automatik"
PAUSE_TIMER   = "timer.klima_automatik_pause"
HAUS_KUEHLEN  = "script.klimahauskuhlen"

# Saisonlogik mit Hysterese (Aussentemperatur)
KUEHLSAISON_AN  = 21.0   # darueber beginnt die Kuehlsaison
KUEHLSAISON_AUS = 19.0   # erst darunter endet sie wieder
HEIZSAISON_AN   = 15.0   # darunter beginnt die Heizsaison
HEIZSAISON_AUS  = 17.0   # erst darueber endet sie wieder

# Regelung um die Zieltemperatur (Innensensor)
HYSTERESE          = 0.5   # Einschaltschwelle: Ziel +/- 0.5
UNTERSCHIESS_AUS   = 1.5   # Inverter-Aus erst bei deutlichem Ueberschiessen
UEBERSTEUERN_DELTA = 1.5   # Uebergangszone: ab Ziel +/- 1.5 darf trotzdem geregelt werden

MODUSWECHSEL_SPERRE_MIN = 30   # Mindestabstand zwischen heat <-> cool
SENSOR_KARENZ_MIN       = 30   # Aussensensor-Ausfall: letzten Wert so lange weiterverwenden

# Luefter
NACHT_VON     = dtime(22, 0)
NACHT_BIS     = dtime(8, 0)
LUEFTER_NACHT = "Quiet"
LUEFTER_TAG   = "auto"

PAUSE_DAUER          = "02:00:00"
TUER_HALTEZEIT_S     = 120    # Tuer so lange offen -> Haus-Kuehlen
ZIEL_FALLBACK        = 26.0
EIGENBEFEHL_KARENZ_S = 15     # eigene Befehle nie als manuellen Eingriff werten

# =====================  Persistenter Zustand  =====================

state.persist("pyscript.klima_saison", default_value="neutral")          # kuehlen | heizen | neutral
state.persist("pyscript.klima_uebersteuern", default_value="aus")        # aus | cool | heat
state.persist("pyscript.klima_letzter_aktiver_modus", default_value="")  # cool | heat
state.persist("pyscript.klima_moduswechsel_zeit", default_value="")

_aussen_letzter = None
_aussen_zeit = datetime.now()
_letzter_befehl_zeit = None
_meldungen = set()

# =====================  Hilfsfunktionen  =====================

def _f(entity_id, default=None):
    """Zustand als float, sonst default (deckt unavailable/unknown/fehlend ab)."""
    try:
        return float(state.get(entity_id))
    except Exception:
        return default


def _s(entity_id, default=None):
    """Zustand als String; default statt Fehler, wenn die Entitaet fehlt."""
    try:
        return state.get(entity_id)
    except Exception:
        return default


def _melden(schluessel, titel, text):
    if schluessel in _meldungen:
        return
    _meldungen.add(schluessel)
    persistent_notification.create(title=titel, message=text,
                                   notification_id=f"klima_{schluessel}")
    log.warning(f"{titel}: {text}")


def _entwarnen(schluessel):
    if schluessel in _meldungen:
        _meldungen.discard(schluessel)
        persistent_notification.dismiss(notification_id=f"klima_{schluessel}")


def _befehl_merken():
    global _letzter_befehl_zeit
    _letzter_befehl_zeit = datetime.now()


def _latch_setzen(wert):
    if state.get("pyscript.klima_uebersteuern") != wert:
        state.set("pyscript.klima_uebersteuern", wert)


def _aussen_lesen():
    """Aussentemperatur; bei Sensorausfall letzter Wert (max. SENSOR_KARENZ_MIN)."""
    global _aussen_letzter, _aussen_zeit
    wert = _f(SENSOR_AUSSEN)
    if wert is not None:
        _aussen_letzter = wert
        _aussen_zeit = datetime.now()
        _entwarnen("aussensensor")
        return wert
    if (datetime.now() - _aussen_zeit).total_seconds() > SENSOR_KARENZ_MIN * 60:
        _melden("aussensensor", "Klima: Aussensensor ausgefallen",
                f"Kein Wert seit ueber {SENSOR_KARENZ_MIN} min. "
                f"Saison bleibt auf '{state.get('pyscript.klima_saison')}', "
                f"Regelung laeuft ueber den Innensensor weiter.")
    return _aussen_letzter


def _saison_bestimmen(aussen):
    """Kuehlsaison ab >21, Ende erst <19; Heizsaison ab <15, Ende erst >17."""
    saison = state.get("pyscript.klima_saison")
    if aussen is None:
        return saison
    if saison == "kuehlen" and aussen < KUEHLSAISON_AUS:
        saison = "neutral"
    elif saison == "heizen" and aussen > HEIZSAISON_AUS:
        saison = "neutral"
    if saison == "neutral":
        if aussen > KUEHLSAISON_AN:
            saison = "kuehlen"
        elif aussen < HEIZSAISON_AN:
            saison = "heizen"
    if saison != state.get("pyscript.klima_saison"):
        log.info(f"Klima: Saisonwechsel -> {saison} (aussen {aussen:.1f} °C)")
        state.set("pyscript.klima_saison", saison)
    return saison

# =====================  Regelentscheidung (Inverter)  =====================

def _regelung(saison, innen, ziel):
    """Liefert (modus, grund); modus ist 'cool', 'heat' oder 'off'.

    Inverter-Philosophie: einmal an, bleibt die Anlage im Modus und regelt
    selbst. Aus geht sie nur bei Saisonende, Sicherheits-Aus oder wenn der
    Raum deutlich ueber das Ziel hinausschiesst."""
    ist = _s(KLIMA)

    if saison == "kuehlen":
        _latch_setzen("aus")
        if ist == "cool":
            if innen < ziel - UNTERSCHIESS_AUS:
                return "off", f"innen {innen:.1f} deutlich unter Ziel (−{UNTERSCHIESS_AUS} K)"
            return "cool", "Kuehlsaison, Inverter haelt Ziel"
        if innen > ziel + HYSTERESE:
            return "cool", f"innen {innen:.1f} > Ziel+{HYSTERESE}"
        return "off", "Kuehlsaison, aktuell kein Bedarf"

    if saison == "heizen":
        _latch_setzen("aus")
        if ist == "heat":
            if innen > ziel + UNTERSCHIESS_AUS:
                return "off", f"innen {innen:.1f} deutlich ueber Ziel (+{UNTERSCHIESS_AUS} K)"
            return "heat", "Heizsaison, Inverter haelt Ziel"
        if innen < ziel - HYSTERESE:
            return "heat", f"innen {innen:.1f} < Ziel−{HYSTERESE}"
        return "off", "Heizsaison, aktuell kein Bedarf"

    # Uebergangszone: Innentemperatur darf uebersteuern (mit Latch als Hysterese)
    latch = state.get("pyscript.klima_uebersteuern")
    if latch == "cool":
        if innen <= ziel:
            _latch_setzen("aus")
            return "off", "Uebersteuern (Kuehlen) beendet, Ziel erreicht"
        return "cool", "Uebergangszone, Uebersteuern aktiv (zu warm)"
    if latch == "heat":
        if innen >= ziel:
            _latch_setzen("aus")
            return "off", "Uebersteuern (Heizen) beendet, Ziel erreicht"
        return "heat", "Uebergangszone, Uebersteuern aktiv (zu kalt)"
    if innen > ziel + UEBERSTEUERN_DELTA:
        _latch_setzen("cool")
        return "cool", f"Uebergangszone, aber innen {innen:.1f} > Ziel+{UEBERSTEUERN_DELTA}"
    if innen < ziel - UEBERSTEUERN_DELTA:
        _latch_setzen("heat")
        return "heat", f"Uebergangszone, aber innen {innen:.1f} < Ziel−{UEBERSTEUERN_DELTA}"
    return "off", "Uebergangszone, kein Bedarf"

# =====================  Befehle an die Anlage  =====================

def _ausschalten(grund):
    if _s(KLIMA) != "off":
        _befehl_merken()
        climate.set_hvac_mode(entity_id=KLIMA, hvac_mode="off")
        log.info(f"Klima AUS -> {grund}")


def _einschalten(modus, ziel, grund):
    # Umschaltsperre heat <-> cool
    letzter = state.get("pyscript.klima_letzter_aktiver_modus")
    if letzter and letzter != modus:
        try:
            delta = (datetime.now() - datetime.fromisoformat(
                state.get("pyscript.klima_moduswechsel_zeit"))).total_seconds()
        except Exception:
            delta = None
        if delta is not None and delta < MODUSWECHSEL_SPERRE_MIN * 60:
            log.info(f"Klima: Wechsel {letzter} -> {modus} gesperrt "
                     f"({delta / 60:.0f} von {MODUSWECHSEL_SPERRE_MIN} min)")
            return

    ist_modus = _s(KLIMA)
    try:
        ist_ziel = float(state.get(f"{KLIMA}.temperature"))
    except Exception:
        ist_ziel = None
    if ist_modus != modus or ist_ziel is None or abs(ist_ziel - ziel) > 0.05:
        _befehl_merken()
        climate.set_temperature(entity_id=KLIMA, hvac_mode=modus, temperature=ziel)
        log.info(f"Klima {modus.upper()} {ziel:.1f} °C -> {grund}")
    if letzter != modus:
        state.set("pyscript.klima_letzter_aktiver_modus", modus)
        state.set("pyscript.klima_moduswechsel_zeit", datetime.now().isoformat())


def _luefter():
    """Nachts Quiet, tagsuebers auto — nur wenn die Anlage laeuft."""
    if _s(KLIMA) == "off":
        return
    jetzt = datetime.now().time()
    nachts = jetzt >= NACHT_VON or jetzt < NACHT_BIS
    soll = LUEFTER_NACHT if nachts else LUEFTER_TAG
    try:
        ist = state.get(f"{KLIMA}.fan_mode")
    except Exception:
        ist = None
    if ist != soll:
        _befehl_merken()
        climate.set_fan_mode(entity_id=KLIMA, fan_mode=soll)
        log.info(f"Klima Luefter -> {soll} ({'Nacht' if nachts else 'Tag'})")

# =====================  Hauptsteuerung  =====================

@time_trigger("startup", "cron(*/5 * * * *)", "cron(0 22 * * *)", "cron(0 8 * * *)")
@state_trigger(SENSOR_INNEN, SENSOR_AUSSEN, ZIEL_HELFER, AUTOMATIK,
               PERSONEN[0], PERSONEN[1], FENSTER[0], FENSTER[1])
@event_trigger("timer.finished", f"entity_id == '{PAUSE_TIMER}'")
def klima_hauptsteuerung(**kwargs):
    task.unique("klima_hauptsteuerung")

    if _s(AUTOMATIK) != "on":
        return

    # Sicherheits-Aus schlaegt alles, auch eine laufende Pause
    fenster_offen = [f for f in FENSTER if _s(f) == "on"]
    personen = {p: _s(p) for p in PERSONEN}
    bekannt = {p: z for p, z in personen.items() if z is not None}
    if not bekannt:
        _melden("anwesenheit", "Klima: Personen-Entitaeten fehlen",
                f"Keine der Entitaeten {', '.join(PERSONEN)} existiert — die "
                f"Abwesenheits-Abschaltung ist ausser Funktion. "
                f"Entity-IDs im Konfigurationsblock von klima.py anpassen.")
        abwesend = False
    else:
        _entwarnen("anwesenheit")
        abwesend = not any([z == "home" for z in bekannt.values()])
    if fenster_offen or abwesend:
        grund = "Fenster offen" if fenster_offen else "niemand zuhause"
        _ausschalten(f"{grund} (Sicherheits-Aus)")
        return

    if _s(PAUSE_TIMER) == "active":
        return

    innen = _f(SENSOR_INNEN)
    if innen is None:
        _melden("innensensor", "Klima: Innensensor ausgefallen",
                "Ohne Innentemperatur keine Regelung — Anlage wurde ausgeschaltet.")
        _ausschalten("Innensensor nicht verfuegbar")
        return
    _entwarnen("innensensor")

    ziel = _f(ZIEL_HELFER, ZIEL_FALLBACK)
    aussen = _aussen_lesen()
    saison = _saison_bestimmen(aussen)

    modus, grund = _regelung(saison, innen, ziel)
    aussen_txt = f"{aussen:.1f} °C" if aussen is not None else "unbekannt"
    zusatz = f"(Saison {saison}, aussen {aussen_txt}, innen {innen:.1f} °C, Ziel {ziel:.1f} °C)"
    if modus == "off":
        _ausschalten(f"{grund} {zusatz}")
    else:
        _einschalten(modus, ziel, f"{grund} {zusatz}")
    _luefter()

# =====================  Manuelle Pause  =====================

@event_trigger("state_changed", f"entity_id == '{KLIMA}'")
def klima_manueller_eingriff(entity_id=None, old_state=None, new_state=None, **kwargs):
    """Eingriff ueber die HA-Oberflaeche (Kontext mit user_id) -> 2 h Pause.

    Eigene Befehle des Scripts werden doppelt ausgefiltert: ueber die
    fehlende user_id und ueber die Karenzzeit nach dem letzten eigenen Befehl."""
    if _s(AUTOMATIK) != "on":
        return
    if old_state is None or new_state is None:
        return
    if (_letzter_befehl_zeit is not None
            and (datetime.now() - _letzter_befehl_zeit).total_seconds() < EIGENBEFEHL_KARENZ_S):
        return
    if new_state.context.user_id is None:
        return  # Geraete-Update oder Automation, kein Mensch ueber die HA-UI

    # nur echte Bedienaenderungen, keine reinen Messwert-Updates
    alt, neu = old_state.attributes, new_state.attributes
    if (new_state.state == old_state.state
            and alt.get("temperature") == neu.get("temperature")
            and alt.get("fan_mode") == neu.get("fan_mode")):
        return

    timer.start(entity_id=PAUSE_TIMER, duration=PAUSE_DAUER)
    log.info(f"Klima: manueller Eingriff erkannt -> Automatik {PAUSE_DAUER} pausiert")

# =====================  Tuer -> Haus kuehlen  =====================

@state_trigger(f"{TUER} == 'on'", state_hold=TUER_HALTEZEIT_S)
def klima_tuer_haus_kuehlen(**kwargs):
    timer.start(entity_id=PAUSE_TIMER, duration=PAUSE_DAUER)
    script.turn_on(entity_id=HAUS_KUEHLEN)
    log.info("Klima: Tuer 2 min offen -> Haus-Kuehlen gestartet, Automatik 2 h pausiert")
