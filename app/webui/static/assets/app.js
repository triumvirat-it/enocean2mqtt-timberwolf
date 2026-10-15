// enocean2mqtt Web-UI — Vue3 SPA (Composition API)
// Bedient die /api/* Endpoints des FastAPI-Backends.

const { createApp, ref, reactive, computed, onMounted, onUnmounted } = Vue;

// ===== API-Wrapper =====

// Liest die "detail"-Meldung aus einer Fehler-Response (FastAPI-Konvention)
// und faellt auf "status statusText" zurueck wenn nichts Lesbares da ist.
async function _extractError(response) {
    try {
        const j = await response.json();
        if (j && j.detail) {
            if (typeof j.detail === "string") return j.detail;
            return JSON.stringify(j.detail);
        }
    } catch (e) { /* nicht JSON, ignorieren */ }
    return response.status + " " + response.statusText;
}

const api = {
    async get(path) {
        const r = await fetch(path);
        if (!r.ok) throw new Error(r.status + " " + r.statusText);
        return await r.json();
    },
    async post(path, body) {
        const r = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: body !== undefined ? JSON.stringify(body) : undefined,
        });
        if (!r.ok) throw new Error(await _extractError(r));
        return await r.json();
    },
    async put(path, body) {
        const r = await fetch(path, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(await _extractError(r));
        return await r.json();
    },
    async del(path) {
        const r = await fetch(path, { method: "DELETE" });
        if (!r.ok && r.status !== 204) throw new Error(r.status + " " + r.statusText);
    },
    async upload(path, file) {
        const fd = new FormData();
        fd.append("file", file);
        const r = await fetch(path, { method: "POST", body: fd });
        if (!r.ok) throw new Error(r.status + " " + r.statusText);
        return await r.json();
    },
};

// ===== Helpers =====
function timeStr(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function rssiStatus(rssi) {
    if (rssi === null || rssi === undefined) return "muted";
    if (rssi >= -65) return "ok";
    if (rssi >= -85) return "warn";
    return "error";
}
function gwStatus(s) {
    const v = (s.runtime && s.runtime.status) || "disconnected";
    if (v === "connected") return "ok";
    if (v === "connecting") return "warn";
    if (v === "disabled") return "muted";
    return "error";
}

// ===== FAM14-Liste (M91: serverseitig im /data, nicht mehr localStorage) =====
// Die FAM14-Modulliste lag frueher pro Browser in localStorage — dadurch sah
// das Handy eine leere Liste, obwohl der Desktop welche hatte. Jetzt zentral
// ueber /api/fam14.

// Speichert die FAM14-Liste serverseitig. Gibt die vom Server validierte/
// normalisierte Liste zurueck, oder null bei Fehler.
async function saveFam14List(items) {
    try {
        const j = await api.put("/api/fam14", { items: items });
        return (j && Array.isArray(j.items)) ? j.items : items;
    } catch (e) { return null; }
}

// Laedt die FAM14-Liste vom Server. Migriert einmalig eine alte Browser-Liste
// (localStorage "fam14_list_v1") auf den Server, falls dort noch nichts liegt —
// so geht die am Desktop gepflegte Liste beim Umstieg nicht verloren.
async function loadFam14List() {
    let items = [];
    try {
        const j = await api.get("/api/fam14");
        items = (j && Array.isArray(j.items)) ? j.items : [];
    } catch (e) { items = []; }
    if (!items.length) {
        let legacy = [];
        try {
            const raw = localStorage.getItem("fam14_list_v1");
            legacy = raw ? JSON.parse(raw) : [];
        } catch (e) { legacy = []; }
        if (Array.isArray(legacy) && legacy.length) {
            const saved = await saveFam14List(legacy);
            if (saved) {
                items = saved;
                // Server ist jetzt Quelle der Wahrheit → alte Browser-Liste weg
                try { localStorage.removeItem("fam14_list_v1"); } catch (e) { /* egal */ }
            }
        }
    }
    return items;
}

// ===== Shared State =====
const state = reactive({
    info: { version: "" },
    toast: null,
    // EEP-Profile aus /api/profiles — Single Source of Truth fuer
    // UI-Kind, Field-Defs (label, unit, icon, enum_labels).
    profiles: [],   // raw list von /api/profiles
    profilesByEep: {},  // eep_id -> profile
});

async function loadProfiles() {
    try {
        const list = await api.get("/api/profiles");
        state.profiles = list;
        const map = {};
        for (const p of list) map[p.eep_id.toUpperCase()] = p;
        state.profilesByEep = map;
    } catch (e) {
        console.warn("Profile-Load fehlgeschlagen:", e);
    }
}

function getProfile(eep) {
    if (!eep) return null;
    return state.profilesByEep[eep.toUpperCase()] || null;
}

function getFieldDef(channel) {
    // Wenn der Channel ein Split-Channel ist (meta.field gesetzt),
    // pickt er sich das passende Feld aus dem Profile.
    const profile = getProfile(channel.eep);
    if (!profile) return null;
    const fieldName = (channel.meta && channel.meta.field) || null;
    if (fieldName) {
        return (profile.fields || []).find(f => f.name === fieldName) || null;
    }
    return null;
}

function formatFieldValue(fdef, value) {
    if (value === null || value === undefined) return "—";
    if (fdef.kind === "bool" || fdef.kind === "enum") {
        const labels = fdef.enum_labels || {};
        // Python-JSON serialisiert bool-Keys als "True"/"False" (capital),
        // JS' String(true) produziert "true" (lowercase). Beides probieren.
        let lbl = labels[String(value)];
        if (lbl === undefined && typeof value === "boolean") {
            lbl = labels[value ? "True" : "False"];
        }
        if (lbl !== undefined) return lbl;
        return String(value);
    }
    if (typeof value === "number") {
        let s = (fdef.decimals != null)
            ? value.toFixed(fdef.decimals)
            : String(value);
        if (fdef.unit) s += " " + fdef.unit;
        return s;
    }
    return String(value);
}

function toast(text, type) {
    state.toast = { text: text, type: type || "" };
    setTimeout(function () { state.toast = null; }, 3500);
}

// ===== Components =====

const TabDashboard = {
    setup() {
        const diag = ref({});
        const gws = ref([]);
        let timer = null;

        async function refresh() {
            try {
                diag.value = await api.get("/api/diagnostics");
                gws.value = await api.get("/api/gateways");
            } catch (e) { /* silent */ }
        }
        onMounted(() => { refresh(); timer = setInterval(refresh, 2000); });
        onUnmounted(() => clearInterval(timer));

        return { diag, gws, gwStatus, timeStr };
    },
    template: [
        '<div class="page-header">',
        '  <div>',
        '    <h1>Dashboard</h1>',
        '    <div class="subtitle">Übersicht über Gateways, Empfang und Cascade</div>',
        '  </div>',
        '</div>',
        '<div class="cards-grid">',
        '  <div class="stat-card">',
        '    <div class="label">Aktive Gateways</div>',
        '    <div class="value">{{ gws.filter(g => g.enabled).length }} / {{ gws.length }}</div>',
        '    <div class="sub">{{ gws.filter(g => g.runtime && g.runtime.status === \'connected\').length }} verbunden</div>',
        '  </div>',
        '  <div class="stat-card">',
        '    <div class="label">Telegramme empfangen</div>',
        '    <div class="value">{{ diag.cascade && diag.cascade.received_total || 0 }}</div>',
        '    <div class="sub">{{ diag.cascade && diag.cascade.duplicates_dropped || 0 }} Duplikate gedropt</div>',
        '  </div>',
        '  <div class="stat-card">',
        '    <div class="label">Dedup-Fenster</div>',
        '    <div class="value">{{ diag.cascade && diag.cascade.dedup_window_ms || 200 }}<span style="font-size:1rem; color:var(--muted)">ms</span></div>',
        '    <div class="sub">{{ diag.cascade && diag.cascade.passed_through || 0 }} durchgereicht</div>',
        '  </div>',
        '</div>',
        '<div class="card">',
        '  <h3>Gateway-Status</h3>',
        '  <table>',
        '    <thead><tr><th>Name</th><th>Typ</th><th>Adresse</th><th>Status</th><th>Empfangen</th><th>Gesendet</th></tr></thead>',
        '    <tbody>',
        '      <tr v-for="gw in gws" :key="gw.name">',
        '        <td><strong>{{ gw.name }}</strong></td>',
        '        <td>{{ gw.type }}</td>',
        '        <td class="mono">{{ gw.host }}:{{ gw.port }}</td>',
        '        <td><span :class="[\'status-pill\', \'status-\' + gwStatus(gw)]">{{ (gw.runtime && gw.runtime.status) || \'disconnected\' }}</span></td>',
        '        <td>{{ (gw.runtime && gw.runtime.received) || 0 }}</td>',
        '        <td>{{ (gw.runtime && gw.runtime.sent) || 0 }}</td>',
        '      </tr>',
        '    </tbody>',
        '  </table>',
        '</div>',
    ].join("\n"),
};

const TabDevices = {
    setup() {
        const devices = ref([]);
        const filter = ref("");
        const expanded = ref(new Set());
        // M114: Gateways (inkl. observed_senders) fuer den "ID empfangen, aber
        // nicht zugewiesen"-Hinweis in der Suche.
        const gws = ref([]);

        async function refresh() {
            try { gws.value = await api.get("/api/gateways"); } catch (e) { /* silent */ }
            const fresh = await api.get("/api/devices");
            if (devices.value.length === 0) {
                devices.value = fresh;
                return;
            }
            // In-place merge: pro device_id nur die dynamischen Felder updaten.
            // Damit zerstoert Vue keine offenen Aufklapp-Animationen oder
            // Klick-Listener im Detail-Block.
            const byId = new Map(devices.value.map(d => [d.device_id, d]));
            const seen = new Set();
            for (const fd of fresh) {
                seen.add(fd.device_id);
                const old = byId.get(fd.device_id);
                if (!old) {
                    devices.value.push(fd);
                    continue;
                }
                // Stammdaten ggf. ueberschreiben (kommt nur bei Add/Edit vor)
                old.name = fd.name;
                old.manufacturer = fd.manufacturer;
                old.model = fd.model;
                old.room = fd.room;
                old.floor = fd.floor;
                old.notes = fd.notes;
                // Channels — in-place pro channel_id
                const oldChByCh = new Map((old.channels || []).map(c => [c.channel_id, c]));
                const freshChIds = new Set();
                for (const fc of (fd.channels || [])) {
                    freshChIds.add(fc.channel_id);
                    const oc = oldChByCh.get(fc.channel_id);
                    if (oc) {
                        // Update dynamische Felder
                        oc.last_state = fc.last_state;
                        oc.actor_state = fc.actor_state;
                        oc.observer_states = fc.observer_states;
                        oc.topic = fc.topic;
                        oc.profile = fc.profile;
                        oc.name = fc.name;
                        oc.enocean_id = fc.enocean_id;
                        oc.learned_pair_id = fc.learned_pair_id;
                        oc.via_gateway = fc.via_gateway;
                        oc.observers = fc.observers;
                        oc.controls = fc.controls;
                        oc.direction = fc.direction;
                        // M103-Fix: per-Channel Override-Felder MITmergen, sonst
                        // liest openEdit nach einem Save veraltete (leere) Werte
                        // und es sieht aus, als waere nichts gespeichert worden.
                        oc.floor = fc.floor;
                        oc.room = fc.room;
                        oc.light_group = fc.light_group;
                        oc.light_role = fc.light_role;
                        // M59: senders unbedingt durchreichen — sonst sieht
                        // openEdit nach einem Save den neu hinzugefuegten
                        // Sender NICHT (alter Vue-State haengt fest).
                        oc.senders = fc.senders;
                        // EEP + meta nach Auto-EEP-Wechsel (M47) oder
                        // manuellem switch-eep (M50) ebenfalls durchreichen.
                        oc.eep = fc.eep;
                        oc.meta = fc.meta;
                    } else {
                        old.channels.push(fc);
                    }
                }
                // Entferne Channels die nicht mehr im Backend existieren —
                // sonst bleibt eine via DELETE entfernte Karteileiche (M51)
                // in der Vue-State stehen und taucht beim naechsten Edit-
                // Dialog-Oeffnen wieder auf.
                for (let i = old.channels.length - 1; i >= 0; i--) {
                    if (!freshChIds.has(old.channels[i].channel_id)) {
                        old.channels.splice(i, 1);
                    }
                }
            }
            // Entferne Geraete die nicht mehr da sind
            for (let i = devices.value.length - 1; i >= 0; i--) {
                if (!seen.has(devices.value[i].device_id)) {
                    devices.value.splice(i, 1);
                }
            }
        }
        function toggle(id) {
            if (expanded.value.has(id)) expanded.value.delete(id);
            else expanded.value.add(id);
            expanded.value = new Set(expanded.value);
        }
        // Clipboard-Unterstuetzung wird beim Mount geprueft. Edge auf
        // http://LAN-IP/ blockiert navigator.clipboard ohne Fehler — daher
        // erst per Test-Schreiben pruefen, sonst execCommand-Fallback, sonst
        // Icon ausblenden (clipboardOk = false).
        const clipboardOk = ref(true);
        async function copy(text) {
            // 1) Moderne Clipboard-API
            if (navigator.clipboard && window.isSecureContext) {
                try {
                    await navigator.clipboard.writeText(text);
                    toast("Kopiert: " + text, "success");
                    return;
                } catch (e) { /* fall through */ }
            }
            // 2) Legacy: execCommand("copy") auf hidden textarea
            try {
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.left = "-9999px";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                const ok = document.execCommand("copy");
                document.body.removeChild(ta);
                if (ok) {
                    toast("Kopiert: " + text, "success");
                    return;
                }
            } catch (e) { /* fall through */ }
            // 3) Beides fehlgeschlagen — Icon kuenftig ausblenden
            clipboardOk.value = false;
            toast("Kopieren nicht moeglich (Browser blockiert).", "error");
        }
        // Beim Start: pruefen ob ueberhaupt eine Clipboard-Methode bereit ist.
        if (typeof window !== 'undefined') {
            onMounted(() => {
                const hasModern = navigator.clipboard && window.isSecureContext;
                const hasLegacy = !!document.queryCommandSupported
                    && document.queryCommandSupported("copy");
                if (!hasModern && !hasLegacy) clipboardOk.value = false;
            });
        }
        function topic(d, c) {
            // Bevorzuge das Topic das vom Backend mitgeliefert wird (kommt mit
            // floor/room aus der Device-Definition). Fallback nur wenn das
            // Feld noch nicht da ist (alte Backend-Version).
            if (c && c.topic) return c.topic;
            return "enocean/devices/" + d + "/" + (c && c.channel_id || c) + "/state";
        }
        async function deleteDev(id) {
            if (!confirm('Gerät "' + id + '" wirklich entfernen?')) return;
            await api.del("/api/devices/" + encodeURIComponent(id));
            toast("Gerät entfernt", "success");
            refresh();
        }
        async function testChannel(deviceId, channelId, command) {
            try {
                await api.post(
                    "/api/devices/" + encodeURIComponent(deviceId)
                    + "/channels/" + encodeURIComponent(channelId) + "/test",
                    command,
                );
                toast("Befehl gesendet", "success");
            } catch (e) {
                toast("Senden fehlgeschlagen: " + e.message, "error");
            }
        }

        // Klassifiziert einen Channel — primaer ueber das channel-eigene
        // profile.ui_kind, das das Backend per device_type-Override gesetzt hat
        // (M73 / M75: FSR14 = "switch", FUD14 = "dimmer", obwohl beide A5-38-08).
        // Fallback: globales EEP-Profile (Single Source of Truth fuer EEPs
        // ohne device_type-Disambiguierung).
        //
        // M77c: Der frueher genutzte learned_pair_id-Check war Legacy aus der
        // Pre-Multi-Sender-Zeit (vor M59) und blockierte das Rendern des
        // TX-/Beobachter-Blocks bei allen modernen Aktor-Channels, die diesen
        // veralteten Wert nicht haben. Wurde entfernt — Channel-Typ haengt
        // jetzt nur am ui_kind.
        function channelKind(c) {
            const channelUiKind = c.profile && c.profile.ui_kind;
            if (channelUiKind) return channelUiKind;
            const profile = getProfile(c.eep);
            if (profile && profile.ui_kind) return profile.ui_kind;
            // Letzter Fallback: kein Profile bekannt → konservativ rx
            return "rx";
        }

        // Channel-Typ darf vom Geraet selbst umgestellt werden (z.B. F3Z14D
        // mit PCT14-Tool: Strom -> Gas -> Wasser). In dem Fall wird der EEP
        // hier automatisch synchronisiert, sobald das Geraet ein Lerntelegramm
        // sendet. Helper zeigt, ob dieser Channel davon betroffen ist.
        function channelIsA5_12_family(c) {
            return (c.eep || "").toUpperCase().startsWith("A5-12-");
        }

        // Ist dieser Channel ein AKTOR (etwas das wir per Funkbefehl
        // steuern koennen — Schalter, Dimmer, Rolladen, Heizungsventil)?
        // Reine Sensoren (Stromzaehler, Wetterstation, Bewegungsmelder,
        // Thermostat, Magnetkontakt, Fenstergriff) sind rx-only und haben
        // weder Sende-PTM noch Beobachter.
        function channelIsActor(c) {
            return channelKind(c) !== "rx";
        }
        // Backwards-kompatibel — alter Name
        function channelSupportsObservers(c) {
            return channelIsActor(c);
        }

        // Bus-only Channel: Sender laeuft nur auf RS485, NICHT ueber Funk.
        // Aktuell ausschliesslich FTS14EM (Drehschalter-Konfig hinterlegt das
        // Meta-Flag beim Scaffold). Diese Channels koennen nie ein last_state
        // bekommen — wir blenden Daten-Aktionen aus und zeigen stattdessen
        // einen "nur Bus"-Badge. Die Sender-IDs sind weiterhin als Observer
        // an Aktor-Channels verwendbar.
        function isBusOnlyChannel(c) {
            if (c && c.meta && c.meta.fts14em_mode) return true;
            // Fallback fuer aeltere Devices ohne persistiertes Meta:
            // Heuristik auf die FTS14EM-Quasi-Dezimal-ID-Range
            // 0x00001001..0x00001500 (keine Hex-Buchstaben in dem Bereich).
            const sid = ((c && c.enocean_id) || "").toUpperCase();
            return /^00001[0-5][0-9]{2}$/.test(sid)
                && sid >= "00001001" && sid <= "00001500";
        }

        // Profile-driven Formatter: nutzt FieldDefs aus /api/profiles.
        // - Wenn der Channel ein Split-Channel ist: zeigt nur dieses eine Feld.
        // - Sonst: zeigt alle topic_split_fields des Profiles.
        function formatDecodedForChannel(channel, decoded) {
            if (!decoded || typeof decoded !== 'object') return null;
            const profile = getProfile(channel.eep);
            if (!profile || !profile.fields) {
                // Fallback bei UNKNOWN-EEP: zeige roh
                if (decoded.raw_hex) return decoded.raw_hex;
                return null;
            }
            const fieldName = (channel.meta && channel.meta.field) || null;
            // Bei Split-Channel: NUR das passende Feld zeigen.
            // Priorität: spezifisches Feld im decoded > Legacy value+kind > nichts.
            if (fieldName) {
                const fdef = profile.fields.find(f => f.name === fieldName);
                if (!fdef) return null;
                let v = decoded[fieldName];
                // Legacy-Fallback: wenn decoded.kind zum fieldName matched,
                // nimm decoded.value. (Alte Telegramm-Decoder ohne Feld-Split.)
                if (v === undefined && decoded.kind === fieldName && decoded.value !== undefined) {
                    v = decoded.value;
                }
                if (v === undefined) return null;
                let s = formatFieldValue(fdef, v);
                if (fdef.icon) s = fdef.icon + " " + s;
                return s;
            }
            // Spezialisierte Dimmer-/Switch-Anzeige (M58):
            // Status + Dim-Wert + Speed + Blocked-Warnung — sauber getrennt
            // ohne "zuletzt"-Quirk. Bei Eltako-Dimmern senden Feedback-
            // Telegramme den letzten Dim-Wert mit, auch wenn der Aktor gerade
            // aus ist — das ist KEIN Bug, der Aktor merkt sich den Wert fuer
            // den naechsten An-Befehl. Wir zeigen also klar: was ist der
            // tatsaechliche Stand jetzt, und welcher Dim-Wert ist hinterlegt.
            if (profile.ui_kind === "dimmer" || profile.ui_kind === "switch") {
                const parts = [];
                // Status
                if (decoded.state) parts.push(decoded.state);
                else if (decoded.on === true) parts.push("ON");
                else if (decoded.on === false) parts.push("OFF");
                // Dim-Wert (nur bei Dimmer)
                if (profile.ui_kind === "dimmer"
                        && typeof decoded.dim_percent === "number") {
                    parts.push(decoded.dim_percent + " %");
                }
                // Speed-Label (nur wenn != intern — sonst informationsleer)
                if (decoded.dim_speed_label
                        && decoded.dim_speed_label !== "intern") {
                    parts.push("Speed: " + decoded.dim_speed_label);
                }
                // Warnung bei blockiertem Dimm-Wert (Eltako-spezifisch)
                if (decoded.blocked) parts.push("⚠ blockiert");
                return parts.length ? parts.join(" · ") : null;
            }
            // RPS-Wippentaster-Spezialfall: beim Release zeigen wir das
            // last_press_event (die zuletzt gedrueckte Taste) statt "release",
            // damit der User auch nach dem Loslassen sieht WAS gedrueckt war.
            // Zusammen mit press_duration_ms ergibt das "↑ A oben · 455 ms".
            if (decoded.event === "release" && decoded.last_press_event) {
                const parts = [];
                const evtField = profile.fields.find(f => f.name === "event");
                const lpe = decoded.last_press_event;
                let lbl = lpe;
                if (evtField && evtField.enum_labels && evtField.enum_labels[lpe]) {
                    lbl = evtField.enum_labels[lpe];
                }
                parts.push(lbl);
                if (typeof decoded.press_duration_ms === "number") {
                    parts.push(decoded.press_duration_ms + " ms");
                }
                return parts.join(" · ");
            }
            // Generisch: alle topic_split_fields nacheinander
            const parts = [];
            for (const fdef of profile.fields) {
                if (!fdef.is_topic_split) continue;
                const v = decoded[fdef.name];
                if (v === undefined || v === null) continue;
                let s = formatFieldValue(fdef, v);
                if (fdef.icon) s = fdef.icon + " " + s;
                parts.push(s);
            }
            // RPS-Wippentaster: rocker_1/rocker_2 als Kontext anhaengen, sofern
            // im decoded vorhanden. Im Profil sind sie is_topic_split=false
            // (eigener MQTT-Sub-Topic waere unerwuenscht), aber als Info hier
            // angezeigt damit der User sieht WELCHE Wippe gerade gedrueckt
            // wurde, nicht nur ob "gedrueckt"/"losgelassen".
            const rocker = decoded.rocker_1 || decoded.rocker_2;
            if (rocker && parts.length > 0) {
                parts.push("Wippe " + rocker);
            }
            if (parts.length === 0 && decoded.raw_hex) parts.push(decoded.raw_hex);
            return parts.length ? parts.join(" · ") : null;
        }

        // Expose fuer Headless-Tests
        if (typeof window !== 'undefined') {
            window.__formatDecodedForChannel = formatDecodedForChannel;
        }

        function lastStateSummary(c) {
            if (!c.last_state) return null;
            const ls = c.last_state;
            const decoded = (ls.device && ls.device.decoded) || ls.decoded || null;

            // tele_channel-Filter: bei Multi-Sub-Channel-EEPs (A5-12-01 mit
            // channel-Byte) zeigen wir den letzten Wert nur dann, wenn das
            // letzte Telegramm zum tele_channel dieses Channels passt. Sonst
            // sehen Tarif-0- und Tarif-1-Channels beide das gleiche Telegramm.
            const teleFilter = c.meta && c.meta.tele_channel;
            if (teleFilter !== undefined && teleFilter !== null && decoded) {
                const profile = getProfile(c.eep);
                const tcField = profile && profile.telegram_channel_field;
                if (tcField && decoded[tcField] !== undefined
                            && Number(decoded[tcField]) !== Number(teleFilter)) {
                    // anderer Sub-Channel — wir haben dafuer (noch) keinen Wert
                    return null;
                }
            }

            // field-Filter wird in formatDecodedForChannel angewendet.
            // Wenn der dekodierte Wert fuer DIESES Feld nicht im decoded ist
            // (z.B. Channel field=energy_kwh, aber letztes Telegramm war
            // current_w), liefert formatDecodedForChannel null — dann zeigen
            // wir auch keinen Hex-Fallback, weil der Hex sich nicht auf
            // dieses Feld bezieht.
            const formatted = formatDecodedForChannel(c, decoded);
            const fieldFilter = c.meta && c.meta.field;
            if (fieldFilter && !formatted) return null;

            const age = Math.round((Date.now() / 1000) - ls.ts);
            const ago = age < 60 ? age + "s" : (Math.floor(age / 60) + "m");
            return {
                ago: ago,
                rssi: ls.rssi_dbm,
                payload: ls.payload,
                formatted: formatted,
            };
        }

        function eepLabel(c) {
            // EEP + Profile-Name + Einheit (wenn Single-Field)
            // Bsp: "A5-12-01 · Energiezähler · kWh"
            const profile = getProfile(c.eep);
            const parts = [c.eep];
            if (profile && profile.name) parts.push(profile.name);
            const fdef = getFieldDef(c);
            if (fdef && fdef.unit) parts.push(fdef.unit);
            return parts.join(" · ");
        }

        // ===== ActorState Helpers =====
        function actorState(c) {
            return c.actor_state || null;
        }
        function isCalibrated(c) {
            const s = actorState(c);
            return s ? s.calibrated : false;
        }
        function positionPct(c) {
            const s = actorState(c);
            return s ? Math.round(s.position_percent || 0) : 0;
        }
        function dimPct(c) {
            const s = actorState(c);
            return s ? (s.dim_percent || 0) : 0;
        }
        function isOn(c) {
            const s = actorState(c);
            return s ? !!s.on : false;
        }
        function isMoving(c) {
            const s = actorState(c);
            return s && !!s.moving;
        }
        function movingDirection(c) {
            const s = actorState(c);
            return s ? s.moving : null;
        }

        async function moveToPosition(deviceId, channelId, pct) {
            await testChannel(deviceId, channelId, { position: pct });
        }
        async function dimTo(deviceId, channelId, pct) {
            await testChannel(deviceId, channelId, { dim: pct, state: pct > 0 });
        }
        // M66/M67/M68: Eltako-Dimmer verstehen nur A5-38-08 Command 2.
        // M69: Dim-Speed wird pro Channel persistiert (channel.meta.dim_speed)
        // und bei jedem Befehl mitgegeben. Im UI-Input editierbar; beim Send
        // wird der Wert via Backend in devices.yaml gespeichert.
        function _effectiveDimSpeed(d, c) {
            const k = inputKey(d, c);
            const local = parseInt((inputs[k] || {}).speed);
            if (!isNaN(local) && local >= 0 && local <= 255) return local;
            const persisted = parseInt((c.meta || {}).dim_speed);
            return isNaN(persisted) ? 0 : persisted;
        }
        async function turnDimmerOn(d, c) {
            // An: gemerkten Dimmwert (actor_state.dim_percent) wiederherstellen.
            // KEIN Schaltbefehl (A5-38-08 Command 1) — der FUD14/FLD61 ignoriert
            // ihn (live gemessen 2026-06-06), nur Command 2 (Dimmen) wirkt. Der
            // Memory wird im Backend persistiert (actor_state.yaml) und ueber-
            // lebt den Neustart. 100% nur als Notnagel, wenn kein Wert bekannt.
            let dim = dimPct(c);
            if (!dim || dim <= 0) dim = 100;
            const speed = _effectiveDimSpeed(d, c);
            await testChannel(d.device_id, c.channel_id, {
                dim: dim, ramp: speed, state: true,
            });
        }
        async function turnDimmerOff(d, c) {
            const speed = _effectiveDimSpeed(d, c);
            await testChannel(d.device_id, c.channel_id, {
                dim: 0, ramp: speed, state: false,
            });
        }

        // Lokale Eingabe-Werte pro Channel (für Soll-Feld + Laufzeit-Feld)
        const inputs = reactive({});
        function inputKey(d, c) { return d.device_id + "::" + c.channel_id; }
        function getInput(d, c, field, fallback) {
            const k = inputKey(d, c);
            if (!inputs[k]) inputs[k] = {};
            if (inputs[k][field] === undefined) inputs[k][field] = fallback;
            return inputs[k][field];
        }
        function setInput(d, c, field, value) {
            const k = inputKey(d, c);
            if (!inputs[k]) inputs[k] = {};
            inputs[k][field] = value;
        }

        async function applyTargetPosition(d, c) {
            const k = inputKey(d, c);
            const target = parseInt((inputs[k] || {}).target);
            if (isNaN(target) || target < 0 || target > 100) {
                toast("Soll-Position 0-100 eingeben", "error");
                return;
            }
            await moveToPosition(d.device_id, c.channel_id, target);
        }
        async function applyTravelTime(d, c) {
            const k = inputKey(d, c);
            const s = actorState(c);
            const t = parseFloat((inputs[k] || {}).travelTime);   // Senken (0→100)
            if (isNaN(t) || t < 1 || t > 300) {
                toast("Laufzeit Senken 1-300s", "error");
                return;
            }
            // Heben (100→0) optional; leer/0 = wie Senken
            const tuRaw = (inputs[k] || {}).travelTimeUp;
            const body = { travel_time_s: t };
            if (tuRaw !== undefined && tuRaw !== "" && tuRaw !== null) {
                const tu = parseFloat(tuRaw);
                if (isNaN(tu) || (tu !== 0 && (tu < 1 || tu > 300))) {
                    toast("Laufzeit Heben 0 oder 1-300s", "error");
                    return;
                }
                body.travel_time_up_s = tu;
            }
            await api.post(
                "/api/actor-state/" + encodeURIComponent(d.device_id) +
                "/" + encodeURIComponent(c.channel_id) + "/calibration",
                body
            );
            toast("Laufzeiten gespeichert (Senken " + body.travel_time_s + "s"
                  + (body.travel_time_up_s ? " / Heben " + body.travel_time_up_s + "s" : "")
                  + ")", "success");
            refresh();
        }
        async function applyDimTarget(d, c) {
            const k = inputKey(d, c);
            const target = parseInt((inputs[k] || {}).target);
            if (isNaN(target) || target < 0 || target > 100) {
                toast("Soll-Dim-Wert 0-100 eingeben", "error");
                return;
            }
            // M69: Speed aus lokalem Input mit Fallback auf channel.meta.dim_speed
            const speed = _effectiveDimSpeed(d, c);
            if (speed < 0 || speed > 255) {
                toast("Speed muss 0-255 sein (0=intern, 1=schnell, 255=langsam)", "error");
                return;
            }
            await testChannel(d.device_id, c.channel_id, {
                dim: target, ramp: speed, state: target > 0,
            });
        }

        async function toggleInvertDirection(d, c, checked) {
            // Setzt channel.meta.invert_direction via PUT /api/devices/{id}.
            // Pipeline kompensiert dann decoded.state physisch invertierte
            // Verkabelung (moving_up <-> moving_down, end_up <-> end_down).
            const channels = (d.channels || []).map(ch => {
                const meta = Object.assign({}, ch.meta || {});
                if (ch.channel_id === c.channel_id) {
                    if (checked) meta.invert_direction = true;
                    else delete meta.invert_direction;
                }
                return Object.assign({}, ch, { meta: meta });
            });
            const payload = {
                device_id: d.device_id,
                name: d.name, manufacturer: d.manufacturer, model: d.model,
                room: d.room, floor: d.floor, notes: d.notes,
                channels: channels,
            };
            try {
                await api.put("/api/devices/" + encodeURIComponent(d.device_id), payload);
                // Direkt local mergen damit das Häkchen sofort sitzt
                if (!c.meta) c.meta = {};
                if (checked) c.meta.invert_direction = true;
                else delete c.meta.invert_direction;
                toast(checked ? "Richtung invertiert" : "Richtung normal", "success");
            } catch (e) {
                toast("Fehler: " + e.message, "error");
            }
        }

        async function resyncPosition(d, c, pct) {
            // Manueller Re-Sync: User sieht visuell wo der Rolladen ist
            // und korrigiert die Software-Position dorthin
            await api.post(
                "/api/actor-state/" + encodeURIComponent(d.device_id) +
                "/" + encodeURIComponent(c.channel_id) + "/set-position",
                { position_percent: pct }
            );
            toast("Position synchronisiert: " + pct + "%", "success");
            refresh();
        }

        // ===== Eichfahrt-Modal =====
        const calibrate = ref({
            open: false,
            device: null,    // device-Objekt
            channel: null,   // channel-Objekt
            step: 1,         // 1=oben bestätigen, 2=läuft, 3=zeit eingeben
            travelTimeSec: 25,
        });
        let calibrateTimer = null;

        function openCalibrate(d, c) {
            calibrate.value = { open: true, device: d, channel: c, step: 1, travelTimeSec: 25 };
        }
        function closeCalibrate() {
            if (calibrateTimer) { clearInterval(calibrateTimer); calibrateTimer = null; }
            calibrate.value.open = false;
        }
        async function calibrateMarkTop() {
            // User bestätigt: Rolladen ist ganz oben → Position = 0%
            const { device, channel } = calibrate.value;
            await api.post(
                "/api/actor-state/" + encodeURIComponent(device.device_id)
                + "/" + encodeURIComponent(channel.channel_id) + "/set-position",
                { position_percent: 0 }
            );
            // Befehl "runter mit 60 Sekunden" senden (länger als Laufzeit, damit Endlage erreicht wird)
            await api.post(
                "/api/devices/" + encodeURIComponent(device.device_id)
                + "/channels/" + encodeURIComponent(channel.channel_id) + "/test",
                { command: "down", duration_s: 60 }
            );
            calibrate.value.step = 2;
            calibrate.value.startedAt = Date.now();
            calibrate.value.elapsedSec = 0;
            calibrateTimer = setInterval(() => {
                calibrate.value.elapsedSec = Math.round((Date.now() - calibrate.value.startedAt) / 1000);
            }, 200);
        }
        async function calibrateMarkBottom() {
            // User bestätigt: Rolladen ist unten angekommen
            if (calibrateTimer) { clearInterval(calibrateTimer); calibrateTimer = null; }
            // Erstmal Stop senden (für den Fall dass der noch läuft)
            const { device, channel } = calibrate.value;
            await api.post(
                "/api/devices/" + encodeURIComponent(device.device_id)
                + "/channels/" + encodeURIComponent(channel.channel_id) + "/test",
                { command: "stop" }
            );
            calibrate.value.travelTimeSec = Math.max(5, calibrate.value.elapsedSec || 25);
            calibrate.value.step = 3;
        }
        async function calibrateSave() {
            const { device, channel, travelTimeSec } = calibrate.value;
            await api.post(
                "/api/actor-state/" + encodeURIComponent(device.device_id)
                + "/" + encodeURIComponent(channel.channel_id) + "/calibration",
                { travel_time_s: travelTimeSec }
            );
            await api.post(
                "/api/actor-state/" + encodeURIComponent(device.device_id)
                + "/" + encodeURIComponent(channel.channel_id) + "/set-position",
                { position_percent: 100 }
            );
            toast("Eichfahrt gespeichert: " + travelTimeSec + "s", "success");
            closeCalibrate();
            refresh();
        }

        // M114: Suche nicht nur ueber enocean_id (Empfang), sondern auch ueber
        // Sender- und Beobachter-IDs eines Kanals — und leerzeichen-tolerant.
        // So findet man eine PTM-ID (z.B. "00 2F FB 57") auch dann, wenn sie nur
        // als Beobachter an einem Aktor-Kanal haengt.
        function compactId(s) { return String(s || "").replace(/\s+/g, "").toUpperCase(); }
        function channelIds(c) {
            const ids = [];
            if (c.enocean_id) ids.push(c.enocean_id);
            if (c.learned_pair_id) ids.push(c.learned_pair_id);
            for (const s of (c.senders || [])) if (s && s.sender_id) ids.push(s.sender_id);
            for (const o of (c.observers || [])) {
                const sid = (typeof o === "string") ? o : (o && o.sender_id);
                if (sid) ids.push(sid);
            }
            return ids.map(compactId);
        }
        // Begruendung warum ein Geraet zum ID-Suchbegriff passt (Empfang/Sender/
        // Beobachter) — fuer kleine Kennzeichen in der Trefferliste.
        function matchReasons(d, qc) {
            if (!qc) return [];
            const reasons = new Set();
            for (const c of (d.channels || [])) {
                if (c.enocean_id && compactId(c.enocean_id).includes(qc)) reasons.add("Empfang");
                if (c.learned_pair_id && compactId(c.learned_pair_id).includes(qc)) reasons.add("Sender");
                for (const s of (c.senders || [])) {
                    if (s && s.sender_id && compactId(s.sender_id).includes(qc)) {
                        reasons.add(s.active ? "Sender" : "Beobachter");
                    }
                }
                for (const o of (c.observers || [])) {
                    const sid = (typeof o === "string") ? o : (o && o.sender_id);
                    if (sid && compactId(sid).includes(qc)) reasons.add("Beobachter");
                }
            }
            return [...reasons];
        }

        const filtered = computed(() => {
            const q = filter.value.toLowerCase().trim();
            if (!q) return devices.value;
            const qc = compactId(q);
            return devices.value.filter(d => {
                const channels = d.channels || [];
                const hay = [
                    d.name, d.manufacturer, d.model, d.room, d.device_id,
                ].concat(channels.map(c => c.name || "")).join(" ").toLowerCase();
                if (hay.includes(q)) return true;
                // ID-Treffer (leerzeichen-tolerant) ueber alle Kanal-IDs
                if (qc.length >= 3) {
                    for (const c of channels) {
                        if (channelIds(c).some(id => id.includes(qc))) return true;
                    }
                }
                return false;
            });
        });

        // M114: Hinweis, wenn eine gesuchte ID keinem Geraet zugeordnet ist, aber
        // im Funk empfangen wurde (observed_senders, ausgeliefert pro Gateway).
        function fmtAgoSecs(ts) {
            if (!ts) return "noch nie";
            const age = Math.round((Date.now() / 1000) - ts);
            if (age < 60) return "vor " + age + "s";
            if (age < 3600) return "vor " + Math.floor(age / 60) + " min";
            if (age < 86400) return "vor " + Math.floor(age / 3600) + " h";
            return "vor " + Math.floor(age / 86400) + " d";
        }
        const searchHint = computed(() => {
            const qc = compactId(filter.value.trim());
            // nur fuer ID-artige Eingaben und nur wenn es keine Geraete-Treffer gibt
            if (!qc || qc.length < 4 || !/^[0-9A-F]+$/.test(qc)) return null;
            if (filtered.value.length > 0) return null;
            for (const gw of (gws.value || [])) {
                const obs = (gw.block && gw.block.observed) || [];
                for (const o of obs) {
                    if (compactId(o.sender_id).includes(qc)) {
                        return {
                            found: true,
                            sender_id: o.sender_id,
                            gateway: gw.name,
                            rssi: o.rssi_dbm,
                            ago: fmtAgoSecs(o.last_seen),
                            count: o.count,
                        };
                    }
                }
            }
            return { found: false };
        });

        function channelCount(devs) {
            return devs.reduce((s, d) => s + ((d.channels || []).length), 0);
        }
        function firstChannelTags(d) {
            const c = (d.channels || [])[0];
            return (c && c.meta && c.meta.tags) || [];
        }
        function fileSizeKB(f) {
            return Math.round(f.size / 1024);
        }

        // ===== Edit-Dialog =====
        // Bearbeitet die Stammdaten eines Geraets (Name, Hersteller, Modell, Raum, Floor)
        // sowie die Tags. Tags werden konsistent auf ALLEN Channels gespeichert
        // (gleiche Konvention wie CSV-Import).
        // Modi: "edit" (vorhandenes Geraet aendern) und "add" (manuell anlegen).
        const editDialog = reactive({
            open: false,
            saving: false,
            mode: "edit",
            original_id: "",
            form: {
                device_id: "",  // nur im Add-Modus editierbar
                name: "", manufacturer: "", model: "",
                room: "", floor: "", notes: "",
            },
            tags: [],
            tagInput: "",
            // Channels: Liste pro Channel (channel_id, name, enocean_id, eep,
            // learned_pair_id, observers). Im Add-Modus kann der User Channels frei hinzufuegen.
            channels: [],
            // Modell-Autocomplete fuer Add-Modus
            modelSearch: "",
            modelSuggestions: [],
            selectedProduct: null,
        });
        // Alle Tag-Vorschlaege aus dem Bestand sammeln (Autocomplete)
        const allKnownTags = computed(() => {
            const s = new Set();
            for (const d of devices.value) {
                for (const c of (d.channels || [])) {
                    for (const t of ((c.meta && c.meta.tags) || [])) s.add(t);
                }
            }
            return Array.from(s).sort();
        });
        const tagSuggestions = computed(() => {
            const q = editDialog.tagInput.trim().toLowerCase();
            if (!q) return [];
            return allKnownTags.value.filter(t =>
                t.toLowerCase().includes(q) && !editDialog.tags.includes(t)
            ).slice(0, 6);
        });

        function openEdit(d) {
            editDialog.open = true;
            editDialog.saving = false;
            editDialog.mode = "edit";
            editDialog.original_id = d.device_id;
            // M83/M84b: Pairing-State auch im Edit-Modus initialisieren —
            // sonst crasht das Template bei D2-01/D2-05-Channels (referenziert
            // editDialog.pairing.securityCode → undefined → Render-Fehler →
            // Edit-Dialog laesst sich nicht oeffnen).
            editDialog.pairing = {
                securityCode: "", gateway: "", subAddr: "",
                busy: false, result: "",
            };
            editDialog.form = {
                device_id: d.device_id,
                name: d.name || "",
                manufacturer: d.manufacturer || "",
                model: d.model || "",
                room: d.room || "",
                floor: d.floor || "",
                notes: d.notes || "",
            };
            // product_info aus /api/devices (M53): enthaelt description aus
            // products.yaml inkl. ID-Rechenweg fuer Eltako-Bus-Module.
            editDialog.productInfo = d.product_info || null;
            editDialog.gatewayBaseId = d.gateway_base_id || null;
            // M72b: FAM14-Bus-Anbindung auch im Edit-Modus verfuegbar
            editDialog.fam14Bus = !!(d.product_info && d.product_info.fam14_bus);
            editDialog.fam14BaseId = "";
            editDialog.fam14StartAddress = "";
            editDialog.rxPreview = [];
            // FTS14EM-Erkennung im Edit-Modus
            editDialog.isFts14em = (
                (d.manufacturer || "").toLowerCase() === "eltako"
                && (d.model || "").toUpperCase() === "FTS14EM"
            );
            editDialog.fts14emGroup = 1;
            editDialog.fts14emSubdialPos = 0;
            editDialog.fts14emMode = "UT";
            // Reverse-Detect Gruppe + Drehschalter aus existierender Channel-ID:
            // quasi_dec = group + subdial_pos + (input_nr-1) + 1000.
            if (editDialog.isFts14em && d.channels && d.channels.length) {
                const firstSid = (d.channels[0] && d.channels[0].enocean_id) || "";
                if (/^0000[1-5][0-9]{3}$/i.test(firstSid)) {
                    const dec = parseInt(firstSid.substring(4), 16).toString();
                    // dec ist "1001".."1500" — Stelle 1 = Gruppe-Praefix, Stelle 2-4 = Taster-Nr.
                    if (dec.length === 4 && dec.startsWith("1")) {
                        const taster = parseInt(dec.substring(1));  // 001..500
                        if (taster >= 1 && taster <= 500) {
                            // Gruppe = 1 + 100*floor((taster-1)/100), aus {1,101,201,301,401}
                            const groupIdx = Math.floor((taster - 1) / 100);
                            editDialog.fts14emGroup = 1 + 100 * groupIdx;
                            // Innerhalb der Gruppe: Drehschalter = 10*floor((rest-1)/10)
                            const inGroup = ((taster - 1) % 100) + 1;  // 1..100
                            editDialog.fts14emSubdialPos = Math.floor((inGroup - 1) / 10) * 10;
                            // RT-Modus erkennen: wenn nur 5 Channels und alle gerade Endziffern
                            if (d.channels.length === 5) {
                                editDialog.fts14emMode = "RT";
                            }
                        }
                    }
                }
            }
            // M91: FAM14-Liste serverseitig laden (nicht mehr localStorage),
            // danach Auto-Detect der Base + Bus-Adresse aus den Channels.
            editDialog.fam14List = [];
            loadFam14List().then(items => {
                editDialog.fam14List = items;
                if (editDialog.fam14Bus && items.length) {
                    const firstSid = (d.channels && d.channels[0] && d.channels[0].enocean_id) || "";
                    if (/^[0-9A-F]{8}$/i.test(firstSid)) {
                        const high = firstSid.substring(0, 6).toUpperCase();
                        const matchingFam = items.find(f =>
                            (f.base_id || "").substring(0, 6).toUpperCase() === high
                        );
                        if (matchingFam) {
                            editDialog.fam14BaseId = matchingFam.base_id;
                            // HEX-direct-Reverse (M77): low byte ist die Bus-Adr
                            // in Hex. base|addr => addr = low7bits.
                            const lowHex = parseInt(firstSid.substring(6, 8), 16);
                            const addr = lowHex & 0x7F;
                            if (addr >= 1 && addr <= 127) {
                                editDialog.fam14StartAddress = String(addr);
                            }
                        }
                    }
                }
            });
            editDialog.tags = (firstChannelTags(d) || []).slice();
            editDialog.tagInput = "";
            // Channels editierbar (Observers, IDs koennen nachgepflegt werden);
            // channel_id + eep sind in der UI read-only, damit der User die
            // Topic-Struktur nicht versehentlich bricht.
            editDialog.channels = (d.channels || []).map(c => ({
                channel_id: c.channel_id,
                name: c.name || "",
                // M95: per-Channel Etage/Raum-Override (leer = erbt vom Gerät)
                floor: c.floor || "",
                room: c.room || "",
                // M103: Farbleuchte-Gruppe + Rolle
                light_group: c.light_group || "",
                light_role: c.light_role || "",
                enocean_id: (c.enocean_id || ""),
                eep: c.eep || "UNKNOWN",
                direction: c.direction || "rx",
                // M59: Multi-Sender — wird vom Backend bereits migriert geliefert
                // M60: gateway_match ist die ueber Base-ID-Block ermittelte
                // Gateway-Zuordnung (auch wenn dieses Gateway deaktiviert ist)
                senders: ((c.senders || [])).map(s => ({
                    sender_id: (s.sender_id || "").toUpperCase(),
                    via_gateway: s.via_gateway || "",
                    label: s.label || "",
                    active: !!s.active,
                    gateway_match: s.gateway_match || null,
                })),
                // M77b: profile mit ui_kind vom Backend durchreichen, damit
                // channelKind() im Edit-Dialog das channel-spezifische ui_kind
                // (device_type-Override aus M73) sieht und nicht in den
                // veralteten learned_pair_id-Fallback faellt — sonst zeigt
                // der Edit-Dialog bei FSR14/FUD14 keinen TX-Block.
                profile: c.profile || null,
                // legacy (DEPRECATED) — fuer Kompatibilitaet mit altem Code
                learned_pair_id: (c.learned_pair_id || ""),
                via_gateway: (c.via_gateway || ""),
                observers: ((c.observers || [])).slice(),
                observerInput: "",
                observerMatchSel: "any",
                unit_override: ((c.meta || {}).unit_override) || "",
                meta: Object.assign({}, c.meta || {}),
            }));
            // FTS14EM: bei aelteren Devices fehlt meta.fts14em_mode (vor dem
            // Save-Meta-Fix nicht persistiert). Rekonstruieren aus channel_id
            // (1..10 entspricht E1..E10) — damit erscheint der "nur Bus"-Badge
            // sofort, und beim naechsten Save sind die Felder in devices.yaml.
            if (editDialog.isFts14em) {
                const dataByteUT = [0x70, 0x50, 0x30, 0x10, 0x70, 0x50, 0x30, 0x10, 0x70, 0x50];
                for (const ch of editDialog.channels) {
                    if (!ch.meta.fts14em_mode) {
                        const n = parseInt(ch.channel_id);
                        if (n >= 1 && n <= 10) {
                            ch.meta.fts14em_input = n;
                            ch.meta.fts14em_data_byte = "0x" + dataByteUT[n - 1].toString(16).toUpperCase().padStart(2, "0");
                            ch.meta.fts14em_mode = editDialog.fts14emMode || "UT";
                        }
                    }
                }
            }
            // Gateways laden fuer das via_gateway-Dropdown (M57)
            loadDialogGateways();
        }
        // M83b: laedt ALLE Gateways (auch deaktivierte) fuer Sender/Beobachter-
        // Auswahl. Deaktivierte LAN-GWs (z.B. waehrend einer Migration)
        // muessen als Beobachter-Quelle waehlbar bleiben — ihr Block ist ja
        // bekannt und Telegramme von dort kommen real rein.
        function loadDialogGateways() {
            api.get("/api/gateways").then(g => {
                editDialog.gateways = (g || []).map(x => ({
                    name: x.name, host: x.host, base_id: x.base_id,
                    enabled: x.enabled !== false,
                }));
            }).catch(() => { editDialog.gateways = []; });
        }
        function openAdd() {
            editDialog.open = true;
            editDialog.saving = false;
            editDialog.mode = "add";
            editDialog.original_id = "";
            editDialog.form = {
                device_id: "",
                name: "", manufacturer: "", model: "",
                room: "", floor: "", notes: "",
            };
            editDialog.tags = [];
            editDialog.tagInput = "";
            editDialog.modelSearch = "";
            editDialog.modelSuggestions = [];
            editDialog.selectedProduct = null;
            // M72: FAM14-Bus-Anbindung — wird bei selectProduct gesetzt
            editDialog.fam14Bus = false;
            editDialog.fam14BaseId = "";
            editDialog.fam14StartAddress = "";
            editDialog.rxPreview = [];
            // FTS14EM-Drehschalter-Konfig: bei FTS14EM-Auswahl aktiv
            editDialog.isFts14em = false;
            editDialog.fts14emGroup = 1;
            editDialog.fts14emSubdialPos = 0;
            editDialog.fts14emMode = "UT";
            // M91: FAM14-Liste serverseitig laden (vom ID-Rechner gepflegt)
            editDialog.fam14List = [];
            loadFam14List().then(items => { editDialog.fam14List = items; });
            // Standard: 1 leerer Channel
            editDialog.channels = [_emptyChannel("1")];
            // M83: ReMan-Pairing-State (OPUS Bridge etc.)
            editDialog.pairing = {
                securityCode: "", gateway: "", subAddr: "",
                busy: false, result: "",
            };
            // M83b: Gateways laden (auch deaktivierte) fuer Sender-Auswahl
            loadDialogGateways();
        }
        function _emptyChannel(channelId) {
            return {
                channel_id: channelId,
                name: "",
                floor: "",
                room: "",
                light_group: "",
                light_role: "",
                enocean_id: "",
                eep: "F6-02-01",
                direction: "rx",
                learned_pair_id: "",
                observers: [],
                observerInput: "",
                observerMatchSel: "any",
            };
        }
        function addChannelRow() {
            const next = String((editDialog.channels.length || 0) + 1);
            editDialog.channels.push(_emptyChannel(next));
        }
        function removeChannelRow(idx) {
            editDialog.channels.splice(idx, 1);
        }
        // Auswahlmenue Strom/Gas/Wasser fuer A5-12-XX-Channels im Edit-Dialog.
        // (Modul-Konstante in pipeline.py: remap_a5_12_field — Backend mappt
        // field/Einheit beim switch-eep-Endpoint.)
        const A5_12_EEP_OPTIONS = [
            { value: "A5-12-01", label: "A5-12-01 · Strom (kWh / W)" },
            { value: "A5-12-02", label: "A5-12-02 · Gas (m³ / m³h)" },
            { value: "A5-12-03", label: "A5-12-03 · Wasser (m³ / L/min)" },
        ];
        async function switchChannelEepInDialog(deviceId, ch, newEep) {
            if (!ch || !deviceId) return;
            if (ch.eep === newEep) return;
            try {
                const r = await api.post(
                    "/api/devices/" + encodeURIComponent(deviceId)
                    + "/channels/" + encodeURIComponent(ch.channel_id)
                    + "/switch-eep",
                    { eep: newEep },
                );
                ch.eep = r.eep;
                if (r.field) {
                    ch.meta = Object.assign({}, ch.meta || {}, { field: r.field });
                }
                if (ch.meta && "unit_override" in ch.meta) {
                    const m = Object.assign({}, ch.meta);
                    delete m.unit_override;
                    ch.meta = m;
                    ch.unit_override = "";
                }
                toast("EEP gewechselt: " + r.old_eep + " → " + r.eep, "success");
                refresh();
            } catch (e) {
                // Auf Fehler das alte EEP zurueckspulen (sonst zeigt das Dropdown
                // einen Wert, der serverseitig nicht gesetzt ist)
                toast("EEP-Wechsel fehlgeschlagen: " + e.message, "error");
                ch.eep = ch.eep;  // Vue-Reaktivitaet halten
            }
        }
        // M59: Multi-Sender pro Aktor-Channel
        function addSenderToChannel(ch) {
            if (!ch.senders) ch.senders = [];
            const isFirst = ch.senders.length === 0;
            ch.senders.push({
                sender_id: "",
                via_gateway: "",
                label: "",
                active: isFirst,  // erster ist automatisch aktiv
            });
        }
        // M61: 1-Klick — Sender mit naechster freier ID aus Gateway-Block.
        async function addSenderFromGateway(ch, gatewayName) {
            try {
                const r = await api.get(
                    "/api/gateways/" + encodeURIComponent(gatewayName)
                    + "/next-free-sender-id"
                );
                if (!ch.senders) ch.senders = [];
                const isFirst = ch.senders.length === 0;
                ch.senders.push({
                    sender_id: r.sender_id,
                    via_gateway: gatewayName,
                    label: "",
                    active: isFirst,
                    gateway_match: gatewayName,
                });
                toast("Sender " + r.sender_id + " aus " + gatewayName
                      + " vorbelegt (" + r.free_remaining + " frei)", "success");
            } catch (e) {
                toast("Konnte freie ID nicht holen: " + e.message, "error");
            }
        }
        // Spiegelt find_gateway_for_sender() aus app/sender_routing.py:
        // Eine Sender-ID gehoert zu dem Gateway, in dessen 128er-Block ab der
        // Base-ID sie liegt. Wird beim Tippen mitgerechnet — vorher wurde
        // gateway_match bei jeder Eingabe geleert und erst nach dem Speichern
        // neu bestimmt, sodass waehrend der Eingabe immer die Warnung
        // "liegt in keinem bekannten Gateway-Block" stand, auch wenn die ID
        // voellig richtig war.
        function gatewayFuerSender(senderId) {
            const sid = parseInt(String(senderId || "").trim(), 16);
            if (!isFinite(sid) || String(senderId).trim().length !== 8) return null;
            for (const g of (editDialog.gateways || [])) {
                if (!g.base_id) continue;
                const base = parseInt(g.base_id, 16);
                if (!isFinite(base)) continue;
                // 0xFFFFFF80: alle Bits ausser den unteren 7
                if (((base & 0xFFFFFF80) >>> 0) === ((sid & 0xFFFFFF80) >>> 0)) {
                    return g.name;
                }
            }
            return null;
        }

        function removeSenderFromChannel(ch, idx) {
            if (!ch.senders) return;
            const wasActive = !!ch.senders[idx].active;
            ch.senders.splice(idx, 1);
            // Wenn der aktive entfernt wurde: ersten markieren
            if (wasActive && ch.senders.length > 0) {
                ch.senders[0].active = true;
            }
        }
        function setActiveSender(ch, idx) {
            if (!ch.senders) return;
            for (let i = 0; i < ch.senders.length; i++) {
                ch.senders[i].active = (i === idx);
            }
        }
        // M83: ReMan-Pairing fuer OPUS BRiDGE (D2-01-XX / D2-05-XX).
        // Diese Aktoren lernen NICHT klassisch per Lerntelegramm, sondern via
        // EnOcean Remote Management (Security-Code-Pairing).
        function channelIsReManActor(ch) {
            const eep = (ch.eep || "").toUpperCase();
            return eep.startsWith("D2-01") || eep.startsWith("D2-05");
        }
        async function startReManPairing(ch) {
            const p = editDialog.pairing || {};
            const code = (p.securityCode || "").trim().toUpperCase().replace(/\s+/g, "");
            if (!/^[0-9A-F]{8}$/.test(code)) {
                toast("Security-Code muss 8 Hex-Zeichen sein (vom QR-Code)", "error");
                return;
            }
            // Sender-ID bestimmen: entweder aus manuell gewähltem Sender, oder
            // automatisch die naechste freie aus dem gewählten Gateway holen.
            let senderId = (p.subAddr || "").trim().toUpperCase();
            const gw = p.gateway || "";
            if (!senderId && gw) {
                try {
                    const r = await api.get(
                        "/api/gateways/" + encodeURIComponent(gw) + "/next-free-sender-id"
                    );
                    senderId = r.sender_id;
                } catch (e) {
                    toast("Konnte freie ID nicht holen: " + e.message, "error");
                    return;
                }
            }
            if (!/^[0-9A-F]{8}$/.test(senderId)) {
                toast("Bitte Gateway wählen oder Sender-ID manuell eintragen", "error");
                return;
            }
            p.busy = true;
            p.result = "";
            try {
                const r = await api.post("/api/pairing/reman", {
                    security_code: code,
                    sender_id: senderId,
                    gateway: gw || null,
                    actor_id: (ch.enocean_id || "").trim().toUpperCase() || null,
                    eep: ch.eep || "D2-01-01",
                });
                p.busy = false;
                // Sender-ID als aktiven Sender eintragen
                if (!ch.senders) ch.senders = [];
                const exists = ch.senders.some(s =>
                    (s.sender_id || "").toUpperCase() === senderId);
                if (!exists) {
                    ch.senders.forEach(s => s.active = false);
                    ch.senders.push({
                        sender_id: senderId, via_gateway: gw, label: "OPUS-Pairing",
                        active: true, gateway_match: gw,
                    });
                }
                const tries = r.attempts ? (" (" + r.attempts + " Versuche)") : "";
                if (r.actor_responded) {
                    if (r.actor_id && /^[0-9A-F]{8}$/.test(r.actor_id)
                            && r.actor_id !== "00000000") {
                        ch.enocean_id = r.actor_id;
                    }
                    if (r.linktable_written) {
                        p.result = "🎉 Pairing komplett" + tries + "! Aktor "
                            + (r.actor_id || "") + " hat geantwortet UND Sende-ID "
                            + senderId + " in die LinkTable eingetragen. "
                            + "Speichern → schalten sollte jetzt gehen.";
                    } else {
                        p.result = "⚠ Aktor hat geantwortet" + tries + " (RX "
                            + (r.actor_id || "?") + "), aber LinkTable-Eintrag "
                            + "nicht bestätigt. Schalten testen — falls es nicht "
                            + "geht, Pairing wiederholen.";
                    }
                } else {
                    p.result = "⏱ " + r.attempts + " Versuche gesendet "
                        + "(Sender " + senderId + ") — keine Aktor-Antwort. Aktor "
                        + "frisch unter Spannung setzen und erneut pairen.";
                }
                toast(r.linktable_written ? "Pairing komplett!"
                      : r.actor_responded ? "Aktor antwortet, LinkTable unklar"
                      : "Keine Antwort", "success");
            } catch (e) {
                p.busy = false;
                p.result = "✗ Fehler: " + e.message;
                toast("Pairing fehlgeschlagen: " + e.message, "error");
            }
        }
        async function sendTeachInForSender(deviceId, ch, sender) {
            if (!deviceId) {
                toast("Erst Geraet speichern, dann Lerntelegramm senden", "error");
                return;
            }
            const sid = (sender.sender_id || "").trim().toUpperCase();
            if (!sid || !/^[0-9A-F]{8}$/.test(sid)) {
                toast("Sender-ID muss 8 Hex-Zeichen sein", "error");
                return;
            }
            if (!ch.eep || !ch.eep.startsWith("A5-")) {
                toast("Lerntelegramm-Senden geht nur fuer 4BS-EEPs (A5-XX-XX)", "error");
                return;
            }
            const gwLine = sender.via_gateway
                ? "Gateway: " + sender.via_gateway
                : "Gateway: (Default, erstes verfuegbares)";
            const confirmMsg = "Lerntelegramm jetzt an den Aktor senden?\n\n"
                + "Workflow:\n"
                + "  1. Aktor in LRN-Modus stellen (z.B. FLD61: Drehknopf auf LRN)\n"
                + "  2. OK klicken\n"
                + "  3. Aktor-LED bestaetigt das Anlernen\n"
                + "  4. Drehknopf zurueck auf %-Position\n\n"
                + "Sender-ID: " + sid + "\n"
                + (sender.label ? "Label: " + sender.label + "\n" : "")
                + "EEP: " + ch.eep + "\n"
                + gwLine + "\n\n"
                + "HINWEIS: Damit der Aktor diese ID lernt, MUSS sie hier vorher"
                + " gespeichert sein UND das gewaehlte Gateway muss diese ID"
                + " senden koennen (ID liegt in Gateway-base_id + 0..127).";
            if (!window.confirm(confirmMsg)) return;
            try {
                const r = await api.post(
                    "/api/devices/" + encodeURIComponent(deviceId)
                    + "/channels/" + encodeURIComponent(ch.channel_id)
                    + "/send-teach-in",
                    { sender_id: sid },
                );
                toast(
                    "Lerntelegramm gesendet (" + r.sender_id + " via " + r.gateway
                    + ") — pruefe Aktor-LED",
                    "success",
                );
            } catch (e) {
                toast("Lerntelegramm fehlgeschlagen: " + e.message, "error");
            }
        }

        async function sendTeachInForChannel(ch) {
            if (!ch) return;
            const deviceId = editDialog.original_id;
            if (!deviceId) {
                toast("Erst Geraet speichern, dann Lerntelegramm senden", "error");
                return;
            }
            if (!ch.learned_pair_id || !ch.learned_pair_id.trim()) {
                toast("Erst eine Sende-PTM (Tx) eintragen — die wird angelernt", "error");
                return;
            }
            if (!ch.eep || !ch.eep.startsWith("A5-")) {
                toast("Lerntelegramm-Senden geht nur fuer 4BS-EEPs (A5-XX-XX)", "error");
                return;
            }
            const gwLine = ch.via_gateway
                ? "Gateway: " + ch.via_gateway
                : "Gateway: (erstes verfuegbares)";
            const confirmMsg = "Lerntelegramm jetzt an den Aktor senden?\n\n"
                + "Workflow:\n"
                + "  1. Aktor in LRN-Modus stellen (z.B. FLD61: Drehknopf auf LRN)\n"
                + "  2. OK klicken\n"
                + "  3. Aktor-LED bestaetigt das Anlernen\n"
                + "  4. Drehknopf zurueck auf %-Position\n\n"
                + "Sender-ID (Tx): " + ch.learned_pair_id + "\n"
                + "EEP: " + ch.eep + "\n"
                + gwLine;
            if (!window.confirm(confirmMsg)) return;
            try {
                const r = await api.post(
                    "/api/devices/" + encodeURIComponent(deviceId)
                    + "/channels/" + encodeURIComponent(ch.channel_id)
                    + "/send-teach-in",
                    {}
                );
                toast(
                    "Lerntelegramm gesendet (" + r.sender_id + " via " + r.gateway
                    + ") — pruefe Aktor-LED",
                    "success",
                );
            } catch (e) {
                toast("Lerntelegramm fehlgeschlagen: " + e.message, "error");
            }
        }
        async function deleteChannelInDialog(idx) {
            const ch = editDialog.channels[idx];
            if (!ch) return;
            const deviceId = editDialog.original_id;
            const confirmMsg = "Channel \"" + (ch.name || ch.channel_id)
                + "\" wirklich entfernen?\n\nDas loescht den MQTT-Topic und"
                + " alle gemerkten Zustaende fuer diesen Channel.";
            if (!window.confirm(confirmMsg)) return;
            if (editDialog.mode === "add") {
                // Im Add-Modus existiert das Device noch nicht — lokal entfernen
                editDialog.channels.splice(idx, 1);
                return;
            }
            try {
                await api.del(
                    "/api/devices/" + encodeURIComponent(deviceId)
                    + "/channels/" + encodeURIComponent(ch.channel_id),
                );
                editDialog.channels.splice(idx, 1);
                toast("Channel entfernt", "success");
                refresh();
            } catch (e) {
                toast("Loeschen fehlgeschlagen: " + e.message, "error");
            }
        }
        // Helper: Observer-Eintraege koennen string (legacy "any") oder
        // {sender_id, match}-Objekt sein. Normalisiert beides auf ein Dict.
        function normalizeObserver(o) {
            if (o == null) return { sender_id: "", match: "any" };
            if (typeof o === "string") return { sender_id: o.toUpperCase(), match: "any" };
            return { sender_id: (o.sender_id || "").toUpperCase(), match: o.match || "any" };
        }
        // Lesbare Anzeige des Match-Filters (UI-Dropdown-Labels).
        function observerMatchLabel(m) {
            const map = {
                "any": "Alle",
                "rocker:A": "RT A",
                "rocker:B": "RT B",
                "event:B_top":    "UT 1 (rechts oben)",
                "event:B_bottom": "UT 2 (rechts unten)",
                "event:A_top":    "UT 3 (links oben)",
                "event:A_bottom": "UT 4 (links unten)",
            };
            return map[m] || m;
        }
        function addObserverToChannel(ch) {
            const t = (ch.observerInput || "").trim().toUpperCase();
            if (!t || !/^[0-9A-F]{8}$/.test(t)) {
                toast("Beobachter-ID: 8 Hex-Zeichen (z.B. FF810055)", "error");
                return;
            }
            const m = ch.observerMatchSel || "any";
            // Duplikat-Check ueber normalisierte (sender_id, match)-Form.
            const dup = (ch.observers || []).some(o => {
                const n = normalizeObserver(o);
                return n.sender_id === t && n.match === m;
            });
            if (dup) {
                toast("Beobachter bereits vorhanden (gleiche ID + Filter)", "error");
                return;
            }
            // Bei match='any' speichern wir die alte String-Form (kompatibel
            // mit aelteren Backend-Versionen und alten YAML-Dateien).
            ch.observers.push(m === "any" ? t : { sender_id: t, match: m });
            ch.observerInput = "";
            ch.observerMatchSel = "any";
        }
        function removeObserverFromChannel(ch, idx) {
            ch.observers.splice(idx, 1);
        }
        function onObserverInputKey(ch, ev) {
            if (ev.key === "Enter" || ev.key === ",") {
                ev.preventDefault();
                addObserverToChannel(ch);
            }
        }

        // Modell-Lookup (Autocomplete) — fragt /api/products/search
        async function lookupModels() {
            const q = (editDialog.modelSearch || "").trim();
            if (q.length < 2) {
                editDialog.modelSuggestions = [];
                return;
            }
            try {
                const r = await api.get("/api/products/search?q=" + encodeURIComponent(q) + "&limit=15");
                editDialog.modelSuggestions = r;
            } catch (e) {
                editDialog.modelSuggestions = [];
            }
        }
        async function selectProduct(p) {
            editDialog.selectedProduct = p;
            editDialog.modelSearch = p.manufacturer + " " + p.model;
            editDialog.modelSuggestions = [];
            // Form vorbelegen
            editDialog.form.manufacturer = p.manufacturer || "";
            editDialog.form.model = p.model || "";
            if (!editDialog.form.name) {
                editDialog.form.name = p.model || "";
            }
            // M72: bei FAM14-Bus-Modulen FAM14-Block einblenden
            editDialog.fam14Bus = !!p.fam14_bus;
            // FTS14EM-Erkennung: eigene Drehschalter-Konfig statt FAM14-Bus
            editDialog.isFts14em = (
                (p.manufacturer || "").toLowerCase() === "eltako"
                && (p.model || "").toUpperCase() === "FTS14EM"
            );
            if (editDialog.isFts14em) {
                editDialog.fam14Bus = false;  // FAM14-Section verstecken
                if (!editDialog.fts14emGroup) editDialog.fts14emGroup = 1;
                if (editDialog.fts14emSubdialPos == null) editDialog.fts14emSubdialPos = 0;
                if (!editDialog.fts14emMode) editDialog.fts14emMode = "UT";
            }
            // Channels via Backend-Endpoint generieren — gleiche Logik wie
            // der CSV-Importer (M53). Damit bekommt der F3Z14D seine 6
            // Channels (3 Eingaenge x Energie/Leistung), der DSZ14DRS seine
            // 5 Channels (Tarif 0/1 x Energie/Leistung + Seriennummer), etc.
            await rescaffoldChannels();
        }
        // M72b: Zweistufig — erst Vorschau berechnen, dann auf Klick uebernehmen.
        // editDialog.rxPreviewChannels haelt die Berechnung bis der User
        // explizit auf "Uebernehmen" klickt. Keine Confirm-Popups mehr.
        async function regenerateRxIdsFromFam14() {
            const p = editDialog.productInfo;
            let url = "/api/products/scaffold-channels?manufacturer="
                + encodeURIComponent(editDialog.form.manufacturer || "")
                + "&model=" + encodeURIComponent(editDialog.form.model || "");
            if (editDialog.isFts14em) {
                // FTS14EM: Drehschalter-Konfig
                if (editDialog.fts14emGroup == null
                        || editDialog.fts14emSubdialPos == null) {
                    toast("Gruppe und oberen Drehschalter waehlen", "error");
                    return;
                }
                url += "&fts14em_group=" + editDialog.fts14emGroup
                     + "&fts14em_subdial_pos=" + editDialog.fts14emSubdialPos
                     + "&fts14em_mode=" + encodeURIComponent(editDialog.fts14emMode || "UT");
            } else {
                // FAM14-Bus-Modul
                const famBase = (editDialog.fam14BaseId || "").trim().toUpperCase();
                const busAddr = parseInt(editDialog.fam14StartAddress);
                if (!p || !p.fam14_bus) {
                    toast("FAM14-Bus-Anbindung nicht verfuegbar fuer dieses Modell", "error");
                    return;
                }
                if (!/^[0-9A-F]{8}$/.test(famBase)) {
                    toast("Bitte FAM14 auswaehlen oder Base-ID eintragen", "error");
                    return;
                }
                if (isNaN(busAddr) || busAddr < 1 || busAddr > 99) {
                    toast("Bus-Adresse muss 1-99 sein", "error");
                    return;
                }
                url += "&fam14_base_id=" + encodeURIComponent(famBase)
                     + "&bus_start_address=" + busAddr;
            }
            try {
                const r = await api.get(url);
                editDialog.rxPreview = r.rx_preview || [];
                // Vorschau-Channels merken — werden erst auf "Uebernehmen"-
                // Klick in editDialog.channels uebernommen.
                editDialog.rxPreviewChannels = r.channels || [];
            } catch (e) {
                toast("Konnte Rx-IDs nicht berechnen: " + e.message, "error");
                editDialog.rxPreview = [];
                editDialog.rxPreviewChannels = [];
            }
        }
        function applyRxIdsFromPreview() {
            const previews = editDialog.rxPreviewChannels || [];
            if (!previews.length) {
                toast("Erst Berechnen klicken", "error");
                return;
            }
            const newByCh = new Map(previews.map(c => [c.channel_id, c]));
            let updated = 0;
            for (const ch of editDialog.channels) {
                const nc = newByCh.get(ch.channel_id);
                if (nc && nc.enocean_id) {
                    ch.enocean_id = nc.enocean_id;
                    updated++;
                }
            }
            toast(updated + " Rx-IDs übernommen — Speichern nicht vergessen", "success");
        }

        // M72: bei Aenderungen von FAM14-Base oder Bus-Adresse die Channels
        // neu aufbauen mit den auto-generierten RX-IDs.
        async function rescaffoldChannels() {
            const p = editDialog.selectedProduct;
            if (!p) return;
            let url = "/api/products/scaffold-channels?manufacturer="
                + encodeURIComponent(p.manufacturer || "")
                + "&model=" + encodeURIComponent(p.model || "");
            const famBase = (editDialog.fam14BaseId || "").trim().toUpperCase();
            const busAddr = parseInt(editDialog.fam14StartAddress);
            if (editDialog.fam14Bus && famBase
                    && /^[0-9A-F]{8}$/.test(famBase)
                    && !isNaN(busAddr) && busAddr >= 1 && busAddr <= 99) {
                url += "&fam14_base_id=" + encodeURIComponent(famBase)
                     + "&bus_start_address=" + busAddr;
            }
            // FTS14EM: Drehschalter-Konfig statt FAM14-Adresse
            if (editDialog.isFts14em
                    && editDialog.fts14emGroup != null
                    && editDialog.fts14emSubdialPos != null) {
                url += "&fts14em_group=" + editDialog.fts14emGroup
                     + "&fts14em_subdial_pos=" + editDialog.fts14emSubdialPos
                     + "&fts14em_mode=" + encodeURIComponent(editDialog.fts14emMode || "UT");
            }
            try {
                const r = await api.get(url);
                editDialog.rxPreview = r.rx_preview || [];
                editDialog.channels = (r.channels || []).map(c => ({
                    channel_id: c.channel_id,
                    name: c.name || "",
                    enocean_id: c.enocean_id || "",
                    eep: c.eep || p.eep || "UNKNOWN",
                    direction: c.direction || (p.ui_kind === "rx" ? "rx" : "bi"),
                    // M77b: ui_kind vom Produkt fuer channelKind() im Add
                    profile: { ui_kind: p.ui_kind || "rx" },
                    senders: [],
                    learned_pair_id: c.learned_pair_id || "",
                    observers: c.observers || [],
                    observerInput: "",
                observerMatchSel: "any",
                    meta: c.meta || {},
                }));
            } catch (e) {
                // Fallback (Modell nicht in DB oder Endpoint nicht verfuegbar):
                // alte Naive-Logik mit channel_count, damit nichts blockiert.
                const n = Math.max(1, p.channel_count || 1);
                const tpl = p.channel_name_template || "Kanal {idx}";
                const channels = [];
                for (let i = 1; i <= n; i++) {
                    const name = tpl
                        .replace("{idx0}", String(i - 1))
                        .replace("{idx}", String(i));
                    channels.push({
                        channel_id: (n > 1) ? ("1." + i) : "1",
                        name: name,
                        enocean_id: "",
                        eep: p.eep || "UNKNOWN",
                        direction: p.ui_kind === "rx" ? "rx" : "bi",
                        profile: { ui_kind: p.ui_kind || "rx" },
                        senders: [],
                        learned_pair_id: "",
                        observers: [],
                        observerInput: "",
                observerMatchSel: "any",
                    });
                }
                editDialog.channels = channels;
                editDialog.rxPreview = [];
            }
        }
        function clearProductSelection() {
            editDialog.selectedProduct = null;
            editDialog.modelSearch = "";
            editDialog.modelSuggestions = [];
        }
        function closeEdit() {
            editDialog.open = false;
        }
        // Verfuegbare EEPs aus Profile-Registry (fuer Channel-Dropdown im Add-Modus)
        const availableEeps = computed(() => {
            return (state.profiles || []).map(p => ({
                value: p.eep_id,
                label: p.eep_id + " · " + p.name,
            })).sort((a, b) => a.value.localeCompare(b.value));
        });
        // Normalisiert eine EnOcean-ID auf 8 Hex-Zeichen (Großschrift).
        // Toleriert: Leerzeichen, Bindestriche, vorangestellte 0x/0X, "h"-Suffix,
        // und 10-Hex-Form (Eltako-Format mit Type-Byte vorne, z.B. 07FF800096
        // -> FF800096). Liefert null bei leerem oder ungueltigem Input.
        function normalizeEnoceanId(raw) {
            if (!raw) return null;
            let s = String(raw).trim().toUpperCase()
                .replace(/^0X/, "")
                .replace(/H$/, "")
                .replace(/[\s\-_]/g, "");
            if (!/^[0-9A-F]+$/.test(s)) return null;
            // 10-Hex (Eltako mit Type-Byte) -> letzte 8
            if (s.length === 10) s = s.slice(2);
            if (s.length !== 8) return null;
            return s;
        }

        function deviceIdSlug(s) {
            // Slug-Logik analog Backend: lower, Umlaute, non-alnum->underscore
            if (!s) return "";
            return s.toLowerCase()
                .replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue")
                .replace(/ß/g, "ss").replace(/&/g, "und")
                .replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
        }
        function addTag(tag) {
            const t = (tag || editDialog.tagInput).trim();
            if (!t || editDialog.tags.includes(t)) {
                editDialog.tagInput = "";
                return;
            }
            editDialog.tags.push(t);
            editDialog.tagInput = "";
        }
        function removeTag(idx) {
            editDialog.tags.splice(idx, 1);
        }
        function onTagInputKey(ev) {
            if (ev.key === "Enter" || ev.key === ",") {
                ev.preventDefault();
                addTag();
            } else if (ev.key === "Backspace" && !editDialog.tagInput && editDialog.tags.length) {
                editDialog.tags.pop();
            }
        }
        async function saveEdit() {
            const newTags = editDialog.tags.slice();
            const isAdd = editDialog.mode === "add";

            let channels;
            let device_id;

            if (isAdd) {
                // Channels aus dem Dialog. Mind. 1 Channel mit ID.
                const ch = (editDialog.channels || []).filter(c =>
                    (c.channel_id || "").trim() && (c.eep || "").trim()
                );
                if (!ch.length) {
                    toast("Mindestens ein Channel mit Channel-ID + EEP nötig", "error");
                    return;
                }
                // Validierung & Normalisierung pro Channel
                const cleanedChannels = [];
                for (const c of ch) {
                    const rxRaw = (c.enocean_id || "").trim();
                    const txRaw = (c.learned_pair_id || "").trim();
                    const rxId = rxRaw ? normalizeEnoceanId(rxRaw) : null;
                    const txId = txRaw ? normalizeEnoceanId(txRaw) : null;
                    if (rxRaw && rxId === null) {
                        toast("Channel " + c.channel_id + ": EnOcean-ID '" + rxRaw + "' ist nicht 8 Hex-Zeichen", "error");
                        return;
                    }
                    if (txRaw && txId === null) {
                        toast("Channel " + c.channel_id + ": Sende-PTM '" + txRaw + "' ist nicht 8 Hex-Zeichen", "error");
                        return;
                    }
                    // Vorhandenes Channel-Meta (z.B. fts14em_mode aus Scaffold)
                    // beibehalten — nur tags ueberschreiben/ergaenzen.
                    const chMeta = Object.assign({}, c.meta || {}, { tags: newTags });
                    cleanedChannels.push({
                        channel_id: c.channel_id.trim(),
                        name: (c.name || "").trim() || c.channel_id.trim(),
                        floor: (c.floor || "").trim(),
                        room: (c.room || "").trim(),
                        light_group: (c.light_group || "").trim(),
                        light_role: (c.light_role || "").trim(),
                        enocean_id: rxId,
                        eep: c.eep.trim(),
                        direction: c.direction || "rx",
                        learned_pair_id: txId,
                        observers: (c.observers || []).map(o => typeof o === "string" ? o.toUpperCase() : { sender_id: (o.sender_id || "").toUpperCase(), match: o.match || "any" }),
                        meta: chMeta,
                    });
                }
                channels = cleanedChannels;
                device_id = (editDialog.form.device_id || "").trim()
                    || deviceIdSlug(editDialog.form.name);
                if (!device_id) {
                    toast("Device-ID konnte nicht bestimmt werden — Name eintragen", "error");
                    return;
                }
            } else {
                // Edit: editDialog.channels ist die SINGLE SOURCE OF TRUTH dessen
                // was der User im UI sieht. Channel-Loeschungen (M51) wirken nur
                // dann, wenn wir AUS DIESER Liste serialisieren — nicht aus
                // original.channels (die kommt vom letzten refresh und hat den
                // soeben geloeschten Channel noch). Bonus: manueller EEP-Wechsel
                // (M50) wird ebenfalls korrekt durchgeschrieben, selbst wenn
                // refresh() noch nicht durch war.
                const original = devices.value.find(x => x.device_id === editDialog.original_id);
                if (!original) {
                    toast("Gerät nicht mehr vorhanden", "error");
                    closeEdit();
                    return;
                }
                const originalById = new Map(
                    (original.channels || []).map(c => [c.channel_id, c])
                );
                channels = [];
                for (const ed of (editDialog.channels || [])) {
                    // Original liefert Felder die im Dialog nicht editierbar
                    // sind: direction, via_gateway, controls, restliche meta-Keys.
                    const orig = originalById.get(ed.channel_id) || {};
                    const meta = Object.assign({}, orig.meta || {});
                    meta.tags = newTags;
                    if (ed.unit_override !== undefined) {
                        const u = (ed.unit_override || "").trim();
                        if (u) meta.unit_override = u;
                        else delete meta.unit_override;
                    }
                    // FTS14EM-Meta aus dem Dialog uebernehmen — werden bei
                    // rescaffoldChannels() (Drehschalter geaendert) gesetzt.
                    // So bleibt der "nur Bus"-Badge nach dem Speichern erhalten.
                    if (ed.meta) {
                        for (const k of ["fts14em_input", "fts14em_data_byte", "fts14em_mode", "fts14em_input_pair"]) {
                            if (ed.meta[k] !== undefined) meta[k] = ed.meta[k];
                        }
                    }
                    // PTM-Polung-Override pro Kanal: "" = global -> Key entfernen.
                    if (ed.meta && "ptm_on_press" in ed.meta) {
                        const p = ed.meta.ptm_on_press;
                        if (p === "I" || p === "0") meta.ptm_on_press = p;
                        else delete meta.ptm_on_press;
                    }
                    const rxRaw = (ed.enocean_id || "").trim();
                    const txRaw = (ed.learned_pair_id || "").trim();
                    let rxId = null;
                    let txId = null;
                    if (rxRaw) {
                        rxId = normalizeEnoceanId(rxRaw);
                        if (rxId === null) {
                            toast("Channel " + ed.channel_id + ": EnOcean-ID '" + rxRaw + "' ist nicht 8 Hex-Zeichen", "error");
                            return;
                        }
                    }
                    if (txRaw) {
                        txId = normalizeEnoceanId(txRaw);
                        if (txId === null) {
                            toast("Channel " + ed.channel_id + ": Sende-PTM '" + txRaw + "' ist nicht 8 Hex-Zeichen", "error");
                            return;
                        }
                    }
                    // M59: senders durchschreiben — Single Source of Truth fuer TX
                    const cleanedSenders = [];
                    for (const s of (ed.senders || [])) {
                        const sid = (s.sender_id || "").trim().toUpperCase();
                        if (!sid) continue;  // leere Sender ignorieren
                        if (!/^[0-9A-F]{8}$/.test(sid)) {
                            toast("Channel " + ed.channel_id + ": Sender-ID '"
                                  + sid + "' ist nicht 8 Hex-Zeichen", "error");
                            return;
                        }
                        cleanedSenders.push({
                            sender_id: sid,
                            via_gateway: (s.via_gateway || "").trim() || null,
                            label: s.label || "",
                            active: !!s.active,
                        });
                    }
                    channels.push({
                        channel_id: ed.channel_id,
                        name: (ed.name || "").trim() || orig.name || ed.channel_id,
                        floor: (ed.floor || "").trim(),
                        room: (ed.room || "").trim(),
                        light_group: (ed.light_group || "").trim(),
                        light_role: (ed.light_role || "").trim(),
                        enocean_id: rxId,
                        // M59: senders ist die echte Quelle. learned_pair_id +
                        // via_gateway werden vom Backend automatisch aus dem
                        // aktiven Sender abgeleitet (model_validator).
                        senders: cleanedSenders,
                        learned_pair_id: txId,
                        eep: ed.eep || orig.eep || "UNKNOWN",
                        direction: orig.direction || ed.direction || "rx",
                        via_gateway: (ed.via_gateway && ed.via_gateway.trim())
                            || orig.via_gateway || null,
                        controls: orig.controls || [],
                        observers: (ed.observers || []).map(o => typeof o === "string" ? o.toUpperCase() : { sender_id: (o.sender_id || "").toUpperCase(), match: o.match || "any" }),
                        meta: meta,
                    });
                }
                device_id = editDialog.original_id;
            }

            const payload = {
                device_id: device_id,
                name: editDialog.form.name.trim() || device_id,
                manufacturer: editDialog.form.manufacturer.trim(),
                model: editDialog.form.model.trim(),
                room: editDialog.form.room.trim(),
                floor: editDialog.form.floor.trim(),
                notes: editDialog.form.notes,
                channels: channels,
            };

            editDialog.saving = true;
            try {
                if (isAdd) {
                    await api.post("/api/devices", payload);
                    toast("Gerät angelegt", "success");
                } else {
                    await api.put("/api/devices/" + encodeURIComponent(editDialog.original_id), payload);
                    toast("Gerät gespeichert", "success");
                }
                closeEdit();
                refresh();
            } catch (e) {
                toast("Speichern fehlgeschlagen: " + e.message, "error");
            } finally {
                editDialog.saving = false;
            }
        }

        // Auto-Refresh alle 3s — Daten werden in-place gemerged (siehe refresh
        // oben), damit Klicks/Aufklappen nicht durch DOM-Neubau unterbrochen werden.
        let devicesTimer = null;
        onMounted(() => {
            refresh();
            devicesTimer = setInterval(refresh, 3000);
        });
        onUnmounted(() => { if (devicesTimer) clearInterval(devicesTimer); });

        // Helper fuer Observer-Anzeige im expanded Channel
        function observerSummary(obs) {
            if (!obs.last_seen) return { age: "noch nie", desc: "" };
            const age = Math.round((Date.now() / 1000) - obs.last_seen);
            let ago;
            if (age < 60) ago = age + "s";
            else if (age < 3600) ago = Math.floor(age / 60) + "m";
            else ago = Math.floor(age / 3600) + "h";
            // Kompakte Beschreibung des letzten Zustands
            let desc = "";
            const d = obs.decoded || {};
            // Wippentaster (F6-02-*): bevorzuge event/last_press_event ueber
            // das alte pressed/rocker_1-Format. So sieht der User welche
            // konkrete Taste zuletzt gedrueckt war, auch nach dem Release.
            const evtMap = {
                "A_top":    "↑ A oben",
                "A_bottom": "↓ A unten",
                "B_top":    "↑ B oben",
                "B_bottom": "↓ B unten",
            };
            if (d.event === "release" && d.last_press_event) {
                desc = (evtMap[d.last_press_event] || d.last_press_event);
                if (typeof d.press_duration_ms === "number") {
                    desc += " · " + d.press_duration_ms + " ms";
                }
            } else if (evtMap[d.event]) {
                desc = evtMap[d.event];
            } else if (d.pressed === true) {
                desc = "↧ gedrückt";
                if (d.rocker_1) desc += " " + d.rocker_1;
            } else if (d.pressed === false) {
                desc = "↥ losgelassen";
            } else if (d.motion !== undefined) {
                desc = d.motion ? "Bewegung" : "ruhig";
            } else if (d.on !== undefined) {
                desc = d.on ? "AN" : "AUS";
            } else if (d.raw) {
                desc = d.raw;
            }
            return { age: ago, desc: desc };
        }

        return {
            observerSummary,
            matchReasons, compactId, searchHint,
            devices, filter, filtered, expanded, toggle, copy, topic, clipboardOk,
            deleteDev,
            testChannel, channelCount, firstChannelTags, fileSizeKB,
            channelKind, channelIsActor, channelIsA5_12_family, channelSupportsObservers, isBusOnlyChannel, lastStateSummary, eepLabel,
            actorState, isCalibrated, positionPct, dimPct, isOn, isMoving, movingDirection,
            moveToPosition, dimTo, turnDimmerOn, turnDimmerOff,
            inputs, getInput, setInput,
            applyTargetPosition, applyTravelTime, applyDimTarget, resyncPosition,
            toggleInvertDirection,
            editDialog, tagSuggestions, openEdit, openAdd, closeEdit,
            addTag, removeTag, onTagInputKey, saveEdit,
            addChannelRow, removeChannelRow, availableEeps, deviceIdSlug,
            A5_12_EEP_OPTIONS, switchChannelEepInDialog, deleteChannelInDialog,
            sendTeachInForChannel,
            addSenderToChannel, removeSenderFromChannel, setActiveSender,
            sendTeachInForSender, addSenderFromGateway, gatewayFuerSender,
            channelIsReManActor, startReManPairing,
            addObserverToChannel, removeObserverFromChannel, onObserverInputKey,
            normalizeObserver, observerMatchLabel,
            lookupModels, selectProduct, clearProductSelection, rescaffoldChannels,
            regenerateRxIdsFromFam14, applyRxIdsFromPreview,
        };
    },
    template: [
        '<div class="page-header">',
        '  <div>',
        '    <h1>Geräte</h1>',
        '    <div class="subtitle">{{ devices.length }} angelegte Geräte mit {{ channelCount(devices) }} Kanälen</div>',
        '  </div>',
        '  <div style="display:flex; gap:0.5rem">',
        '    <button class="btn btn-primary" @click="openAdd">+ Gerät</button>',
        '  </div>',
        '</div>',
        '<div class="card" style="margin-bottom: 1rem">',
        '  <input class="input" v-model="filter" placeholder="Suchen (Name, Hersteller, Raum, ID — auch Sender/Beobachter, Tag) …">',
        '</div>',
        '<div class="card" v-if="filtered.length === 0">',
        '  <div class="empty-state">',
        '    <div class="icon">📭</div>',
        '    <p v-if="devices.length === 0">Noch keine Geräte. Lern Geräte einzeln an oder lege sie aus dem Produktkatalog an.</p>',
        '    <template v-else>',
        '      <p>Keine Treffer für "{{ filter }}".</p>',
        '      <p v-if="searchHint && searchHint.found" style="color: var(--muted); font-size: 0.9rem">📡 ID <b class="mono">{{ searchHint.sender_id }}</b> wird auf Gateway <b>{{ searchHint.gateway }}</b> empfangen ({{ searchHint.rssi }} dBm, zuletzt {{ searchHint.ago }}, {{ searchHint.count }}×), ist aber <b>keinem Gerät zugewiesen</b>. Du kannst sie als Beobachter an einem Aktor-Kanal eintragen.</p>',
        '      <p v-else-if="searchHint && !searchHint.found" style="color: var(--muted); font-size: 0.9rem">Diese ID wurde auch nicht im Live-Log empfangen.</p>',
        '    </template>',
        '  </div>',
        '</div>',
        '<div class="card" v-else style="padding: 0">',
        '  <table>',
        '    <thead><tr><th></th><th>Name</th><th>Hersteller / Modell</th><th>Raum</th><th>Kanäle</th><th>Aktion</th></tr></thead>',
        '    <tbody>',
        '      <template v-for="d in filtered" :key="d.device_id">',
        '        <tr @click="toggle(d.device_id)" style="cursor:pointer">',
        '          <td>{{ expanded.has(d.device_id) ? "▾" : "▸" }}</td>',
        '          <td><strong>{{ d.name }}</strong>',
        '            <span v-for="r in matchReasons(d, compactId(filter))" :key="r" class="tag" style="margin-left: 0.35rem; font-size: 0.7rem">{{ r }}</span>',
        '          </td>',
        '          <td>{{ d.manufacturer }} {{ d.model }}</td>',
        '          <td>{{ d.room }}</td>',
        '          <td>{{ (d.channels || []).length }}</td>',
        '          <td>',
        '            <button class="btn btn-ghost btn-sm" title="Bearbeiten" @click.stop="openEdit(d)">✏️</button>',
        '            <button class="btn btn-ghost btn-sm" title="Löschen" @click.stop="deleteDev(d.device_id)">🗑</button>',
        '          </td>',
        '        </tr>',
        '        <tr v-if="expanded.has(d.device_id)">',
        '          <td colspan="6" style="background: var(--tint); padding: 1rem 1.5rem">',
        '            <div style="margin-bottom: 0.5rem">',
        '              <span class="tag" v-for="t in firstChannelTags(d)" :key="t">{{ t }}</span>',
        '            </div>',
        '            <table style="background: white; border-radius: var(--radius-sm)">',
        '              <thead><tr><th>Ch</th><th>Name</th><th>EnOcean-ID</th><th>Typ / EEP</th><th>Letzter Wert</th><th>MQTT-Topic</th><th>Aktion</th></tr></thead>',
        '              <tbody>',
        '                <template v-for="c in d.channels" :key="c.channel_id"><tr>',
        '                  <td class="mono">{{ c.channel_id }}</td>',
        '                  <td>{{ c.name }}</td>',
        '                  <td class="mono">{{ c.enocean_id || "—" }}</td>',
        '                  <td><span class="tag">{{ eepLabel(c) }}</span></td>',
        '                  <td>',
        '                    <span v-if="isBusOnlyChannel(c)" style="color: var(--muted); font-size: 0.75rem; font-style: italic" title="Sender laeuft nur auf dem RS485-Bus, nicht ueber Funk — kein last_state empfangbar.">nicht via Funk</span>',
        '                    <template v-else-if="lastStateSummary(c)">',
        '                      <span v-if="lastStateSummary(c).formatted" style="font-size: 0.82rem"><b>{{ lastStateSummary(c).formatted }}</b></span>',
        '                      <span v-else class="mono" style="font-size: 0.78rem">{{ lastStateSummary(c).payload }}</span>',
        '                      <br><span style="font-size: 0.72rem; color: var(--muted)">vor {{ lastStateSummary(c).ago }} · {{ lastStateSummary(c).rssi }} dBm</span>',
        '                    </template>',
        '                    <span v-else style="color: var(--muted); font-size: 0.85rem">—</span>',
        '                  </td>',
        '                  <td>',
        '                    <span v-if="isBusOnlyChannel(c)" style="color: var(--muted); font-size: 0.85rem" title="Kein MQTT-Publish — dieser Sender laeuft nur ueber den RS485-Bus.">—</span>',
        '                    <template v-else>',
        '                      <code style="font-size:0.74rem">{{ topic(d, c) }}</code>',
        '                      <button v-if="clipboardOk" class="btn btn-ghost btn-sm" @click="copy(topic(d, c))" title="Topic in Zwischenablage kopieren">📋</button>',
        '                    </template>',
        '                  </td>',
        '                  <td>',
        '                    <template v-if="isBusOnlyChannel(c)">',
        '                      <span class="tag" style="background: rgba(255,165,0,0.15); border: 1px solid #ffa500; color: #cc7700; font-size: 0.74rem" :title="\'Sender geht nur ueber RS485-Bus (FTS14EM ohne FTS14FA). Trage diese ID als Beobachter an einem Aktor-Channel ein, um die Verkabelung im System sichtbar zu machen.\'">🚌 nur Bus</span>',
        '                      <div style="font-size: 0.7rem; color: var(--muted); margin-top: 0.2rem" v-if="c.meta && c.meta.fts14em_data_byte">{{ c.meta.fts14em_data_byte }} · {{ c.meta.fts14em_mode }}-Modus</div>',
        '                    </template>',
        '                    <template v-else-if="channelKind(c) === \'shutter\'">',
        '                      <div style="display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.4rem">',
        '                        <button class="btn btn-sm btn-secondary" @click="testChannel(d.device_id, c.channel_id, {command: \'up\', duration_s: 60})">▲</button>',
        '                        <button class="btn btn-sm btn-secondary" @click="testChannel(d.device_id, c.channel_id, {command: \'stop\'})">■</button>',
        '                        <button class="btn btn-sm btn-secondary" @click="testChannel(d.device_id, c.channel_id, {command: \'down\', duration_s: 60})">▼</button>',
        '                        <span v-if="isMoving(c)" style="font-size:0.72rem; color: var(--mint); margin-left:0.4rem">{{ movingDirection(c) === \'up\' ? \'läuft hoch\' : \'läuft runter\' }}</span>',
        '                        <label style="font-size: 0.72rem; color: var(--muted); margin-left:auto; cursor: pointer" :title="\'Aktiv: rauf/runter und Endlagen werden vertauscht (falls Motor verkehrt herum angeklemmt)\'">',
        '                          <input type="checkbox" :checked="!!(c.meta && c.meta.invert_direction)" @change="toggleInvertDirection(d, c, $event.target.checked)" style="vertical-align: middle"> ⇆ Richtung invertieren',
        '                        </label>',
        '                      </div>',
        '                      <div style="display: grid; grid-template-columns: auto auto; gap: 0.3rem 0.6rem; font-size: 0.78rem; align-items: center">',
        '                        <span style="color: var(--muted)">Ist:</span>',
        '                        <span><span class="mono"><b>{{ positionPct(c) }}%</b></span> <button class="btn btn-sm btn-ghost" title="Software-Position auf 0% setzen (Rolladen ist oben)" @click="resyncPosition(d, c, 0)" style="padding:0.15rem 0.4rem; font-size:0.7rem">↺ 0%</button><button class="btn btn-sm btn-ghost" title="Software-Position auf 100% setzen (Rolladen ist unten)" @click="resyncPosition(d, c, 100)" style="padding:0.15rem 0.4rem; font-size:0.7rem">↺ 100%</button></span>',
        '                        <span style="color: var(--muted)">Soll:</span>',
        '                        <span><input type="number" min="0" max="100" class="input" style="width: 4rem; padding: 0.2rem 0.4rem" :value="getInput(d, c, \'target\', positionPct(c))" @input="setInput(d, c, \'target\', $event.target.value)"><button class="btn btn-sm btn-primary" style="margin-left:0.3rem" @click="applyTargetPosition(d, c)">Fahren</button></span>',
        '                        <span style="color: var(--muted)" title="Laufzeit Senken: 0% (oben) → 100% (unten)">Senken ↓:</span>',
        '                        <span><input type="number" min="1" max="300" step="0.5" class="input" style="width: 4rem; padding: 0.2rem 0.4rem" :value="getInput(d, c, \'travelTime\', actorState(c) ? actorState(c).travel_time_s : 25)" @input="setInput(d, c, \'travelTime\', $event.target.value)"> <span style="color: var(--muted)">s</span></span>',
        '                        <span style="color: var(--muted)" title="Laufzeit Heben: 100% (unten) → 0% (oben). Motor braucht meist länger. Leer/0 = wie Senken.">Heben ↑:</span>',
        '                        <span><input type="number" min="0" max="300" step="0.5" class="input" style="width: 4rem; padding: 0.2rem 0.4rem" placeholder="= Senken" :value="getInput(d, c, \'travelTimeUp\', actorState(c) && actorState(c).travel_time_up_s ? actorState(c).travel_time_up_s : \'\')" @input="setInput(d, c, \'travelTimeUp\', $event.target.value)"> <span style="color: var(--muted)">s</span><button class="btn btn-sm btn-ghost" style="margin-left:0.3rem" title="Beide Laufzeiten speichern" @click="applyTravelTime(d, c)">✓</button></span>',
        '                      </div>',
        '                      <div style="font-size: 0.7rem; color: var(--muted); margin-top: 0.3rem; font-style: italic">',
        '                        Position berechnet aus Laufzeit (Aktor kennt sie nicht). Bei Abweichung mit ↺ resynchronisieren.',
        '                      </div>',
        '                    </template>',
        '                    <template v-else-if="channelKind(c) === \'dimmer\'">',
        '                      <template v-if="c.light_role === \'color\'">',
        '                        <div style="display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.4rem">',
        '                          <button class="btn btn-sm btn-success" @click="turnDimmerOn(d, c)">An</button>',
        '                          <button class="btn btn-sm btn-secondary" @click="turnDimmerOff(d, c)">Aus</button>',
        '                          <span v-if="actorState(c)" class="status-pill" :class="isOn(c) ? \'state-pill-on\' : \'state-pill-off\'" style="margin-left: 0.3rem">{{ isOn(c) ? \'AN\' : \'AUS\' }}</span>',
        '                        </div>',
        '                        <div style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.78rem; max-width: 22rem">',
        '                          <span style="color: var(--muted); white-space: nowrap">🌈 Farbe:</span>',
        '                          <input type="range" min="0" max="100" step="1" class="hue-range" style="flex: 1" :value="getInput(d, c, \'target\', dimPct(c))" @input="setInput(d, c, \'target\', $event.target.value)" @change="applyDimTarget(d, c)">',
        '                          <span class="mono" style="white-space: nowrap"><b>{{ isOn(c) ? dimPct(c) + "%" : "AUS" }}</b></span>',
        '                        </div>',
        '                        <div style="font-size: 0.7rem; color: var(--muted); margin-top: 0.3rem; font-style: italic">Farbposition 0-100 % (DALI-Farbkanal) · beim Loslassen wird gesendet</div>',
        '                      </template>',
        '                      <template v-else>',
        '                      <div style="display: flex; align-items: center; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.4rem">',
        '                        <button class="btn btn-sm btn-success" @click="turnDimmerOn(d, c)">An</button>',
        '                        <button class="btn btn-sm btn-secondary" @click="turnDimmerOff(d, c)">Aus</button>',
        '                        <span v-if="actorState(c)" class="status-pill" :class="isOn(c) ? \'state-pill-on\' : \'state-pill-off\'" style="margin-left: 0.3rem">{{ isOn(c) ? \'AN\' : \'AUS\' }}</span>',
        '                      </div>',
        '                      <div style="display: grid; grid-template-columns: 3.5rem auto; gap: 0.3rem 0.6rem; font-size: 0.78rem; align-items: center">',
        '                        <span style="color: var(--muted)">Ist:</span>',
        '                        <span class="mono"><b>{{ isOn(c) ? dimPct(c) + "%" : "AUS" }}</b></span>',
        '                        <span style="color: var(--muted)">Soll %:</span>',
        '                        <span><input type="number" min="0" max="100" class="input" style="width: 4rem; padding: 0.2rem 0.4rem" :value="getInput(d, c, \'target\', dimPct(c))" @input="setInput(d, c, \'target\', $event.target.value)"></span>',
        '                        <span style="color: var(--muted)" :title="\'0 = im Dimmer eingestellt, 1 = sehr schnell, 255 = sehr langsam. Wird beim Senden in devices.yaml gespeichert.\'">Speed:</span>',
        '                        <span><input type="number" min="0" max="255" class="input" style="width: 4rem; padding: 0.2rem 0.4rem" :value="getInput(d, c, \'speed\', (c.meta && c.meta.dim_speed) || 0)" @input="setInput(d, c, \'speed\', $event.target.value)"><span style="color: var(--muted); font-size: 0.7rem; margin-left:0.3rem">0=intern · 1=schnellst · 255=langsamst (persistent)</span></span>',
        '                        <span></span>',
        '                        <span><button class="btn btn-sm btn-primary" @click="applyDimTarget(d, c)">Setzen</button></span>',
        '                      </div>',
        '                      </template>',
        '                    </template>',
        '                    <template v-else-if="channelKind(c) === \'switch\'">',
        '                      <button class="btn btn-sm btn-success" @click="testChannel(d.device_id, c.channel_id, {state: true})">An</button>',
        '                      <button class="btn btn-sm btn-secondary" @click="testChannel(d.device_id, c.channel_id, {state: false})">Aus</button>',
        '                      <div v-if="actorState(c)" style="margin-top:0.3rem">',
        '                        <span class="status-pill" :class="isOn(c) ? \'state-pill-on\' : \'state-pill-off\'">{{ isOn(c) ? \'AN\' : \'AUS\' }}</span>',
        '                      </div>',
        '                    </template>',
        '                    <template v-else-if="channelKind(c) === \'valve\'">',
        '                      <span style="color: var(--muted); font-size: 0.85rem">Heizungsventil</span>',
        '                    </template>',
        '                    <span v-else style="color: var(--muted); font-size: 0.85rem">nur RX</span>',
        '                  </td>',
        '                </tr>',
        '                <tr v-if="channelSupportsObservers(c) && c.observer_states && c.observer_states.length">',
        '                  <td colspan="7" style="background: rgba(0,0,0,0.02); padding: 0.3rem 1rem">',
        '                    <div style="font-size: 0.72rem; color: var(--muted); margin-bottom: 0.2rem">👁 Beobachter (steuern Aktor direkt — wer hat zuletzt geschaltet?):</div>',
        '                    <div style="display: flex; flex-wrap: wrap; gap: 0.5rem">',
        '                      <div v-for="obs in c.observer_states" :key="obs.sender_id" style="font-size: 0.74rem; padding: 0.2rem 0.5rem; background: white; border-radius: var(--radius-sm); border: 1px solid var(--border)">',
        '                        <span class="mono"><b>{{ obs.sender_id }}</b></span>',
        '                        <span v-if="obs.label" style="margin-left: 0.4rem; color: var(--mint)">„{{ obs.label }}"</span>',
        '                        <span v-if="obs.kind === \'inactive_sender\'" style="margin-left: 0.4rem; font-size:0.66rem; color: var(--muted); font-style: italic">· aus inaktivem Sender</span>',
        '                        <span v-if="observerSummary(obs).desc" style="margin-left: 0.4rem; color: var(--korall)">{{ observerSummary(obs).desc }}</span>',
        '                        <span style="margin-left: 0.4rem; color: var(--muted)">· vor {{ observerSummary(obs).age }}</span>',
        '                        <span v-if="obs.rssi_dbm != null" style="margin-left: 0.4rem; color: var(--muted)">· {{ obs.rssi_dbm }} dBm</span>',
        '                      </div>',
        '                    </div>',
        '                  </td>',
        '                </tr></template>',
        '              </tbody>',
        '            </table>',
        '          </td>',
        '        </tr>',
        '      </template>',
        '    </tbody>',
        '  </table>',
        '</div>',
        // @mousedown.self statt @click.self: schliesst nur, wenn die Maus
        // DIREKT auf dem Backdrop gedrueckt wird. Verhindert versehentliches
        // Schliessen, wenn man Text in einem Feld per Drag markiert und die
        // Maus dabei kurz ueber dem Backdrop loslaesst (click-Target = Backdrop).
        '<div v-if="editDialog.open" class="modal-backdrop" @mousedown.self="closeEdit">',
        '  <div class="modal" style="max-width: 42rem; max-height: 90vh; overflow-y: auto">',
        '    <div class="modal-header">',
        '      <h2>{{ editDialog.mode === "add" ? "Gerät hinzufügen" : "Gerät bearbeiten" }}</h2>',
        '      <span class="modal-close" @click="closeEdit">×</span>',
        '    </div>',
        '    <div v-if="editDialog.mode === \'edit\'" style="color: var(--muted); font-size: 0.78rem; margin-bottom: 1rem">ID: <span class="mono">{{ editDialog.original_id }}</span></div>',
        // Produkt-Doku-Box (M53) — zeigt description aus products.yaml inkl.
        // Eltako-Sender-ID-Rechenweg. Greift bei jedem Geraet mit bekanntem
        // Modell, unabhaengig von device.notes (die ist meist aus altem Import).
        '    <details v-if="editDialog.mode === \'edit\' && editDialog.productInfo && editDialog.productInfo.description" style="margin-bottom: 1rem; padding: 0.5rem 0.7rem; background: rgba(64,224,208,0.06); border-left: 3px solid var(--mint); border-radius: var(--radius-sm); font-size: 0.78rem">',
        '      <summary style="cursor: pointer; color: var(--muted); user-select: none"><b>📖 Doku zu {{ editDialog.form.manufacturer }} {{ editDialog.form.model }}</b></summary>',
        '      <pre style="margin: 0.5rem 0 0 0; font-family: inherit; white-space: pre-wrap; line-height: 1.45">{{ editDialog.productInfo.description }}</pre>',
        '    </details>',
        // M72b: FAM14-Bus-Anbindung im EDIT-Modus — nur bei product.fam14_bus.
        // Auto-Detect bei openEdit prefilled die Felder wenn moeglich.
        // Button "Rx-IDs neu berechnen" wirkt nur auf enocean_id-Felder.
        // FTS14EM-Drehschalter-Konfig (Edit-Modus). Reverse-Detect aus erster Channel-ID beim Oeffnen.
        '    <div v-if="editDialog.mode === \'edit\' && editDialog.isFts14em" style="margin-bottom: 1rem; padding: 0.6rem 0.7rem; background: rgba(255,165,0,0.08); border-left: 3px solid #ffa500; border-radius: var(--radius-sm); font-size: 0.78rem">',
        '      <div style="margin-bottom: 0.4rem"><b>🎛 FTS14EM-Drehschalter</b> — Sender-IDs aus Gruppe + oberem Drehschalter neu berechnen. Quasi-Dezimal-Schema (0x10XX..0x14XX).</div>',
        '      <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.4rem 0.6rem; align-items: center">',
        '        <label>Gruppe</label>',
        '        <select class="input" v-model.number="editDialog.fts14emGroup" style="max-width: 12rem">',
        '          <option :value="1">1 — Taster 1..100</option>',
        '          <option :value="101">101 — Taster 101..200</option>',
        '          <option :value="201">201 — Taster 201..300</option>',
        '          <option :value="301">301 — Taster 301..400</option>',
        '          <option :value="401">401 — Taster 401..500</option>',
        '        </select>',
        '        <label>Oberer Drehschalter</label>',
        '        <select class="input" v-model.number="editDialog.fts14emSubdialPos" style="max-width: 12rem">',
        '          <option v-for="p in [0,10,20,30,40,50,60,70,80,90]" :key="p" :value="p">{{ p }}</option>',
        '        </select>',
        '        <label>Modus</label>',
        '        <select class="input" v-model="editDialog.fts14emMode" style="max-width: 18rem">',
        '          <option value="UT">UT — Universaltaster (10 Channels)</option>',
        '          <option value="RT">RT — Richtungstaster (5 gepaarte Wippen)</option>',
        '        </select>',
        '        <span></span>',
        '        <button class="btn btn-secondary btn-sm" @click="regenerateRxIdsFromFam14" style="max-width: 18rem">🔄 Sender-IDs berechnen (Vorschau)</button>',
        '      </div>',
        '      <div v-if="editDialog.rxPreview && editDialog.rxPreview.length" style="margin-top: 0.5rem; font-size: 0.72rem">',
        '        <b>Vorschau:</b>',
        '        <div style="display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.2rem">',
        '          <span v-for="p in editDialog.rxPreview" :key="p.input" class="tag mono">E{{ p.input }} → {{ p.sender_id }}</span>',
        '        </div>',
        '        <div style="margin-top: 0.5rem">',
        '          <button class="btn btn-primary btn-sm" @click="applyRxIdsFromPreview">✓ In Channels übernehmen</button>',
        '        </div>',
        '      </div>',
        '    </div>',
        '    <div v-if="editDialog.mode === \'edit\' && editDialog.fam14Bus" style="margin-bottom: 1rem; padding: 0.6rem 0.7rem; background: rgba(64,224,208,0.08); border-left: 3px solid var(--mint); border-radius: var(--radius-sm); font-size: 0.78rem">',
        '      <div style="margin-bottom: 0.4rem"><b>🔌 FAM14-Bus-Anbindung</b> — Rx-IDs aus FAM14-Base + PCT14-Bus-Adresse (HEX-direkt) neu berechnen. Name, Sender (Tx), Beobachter bleiben unverändert.</div>',
        '      <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.4rem 0.6rem; align-items: center">',
        '        <label>FAM14</label>',
        '        <div style="display: flex; gap: 0.3rem; align-items: center">',
        '          <select class="input" v-model="editDialog.fam14BaseId" style="max-width: 18rem">',
        '            <option value="">— manuell eintragen —</option>',
        '            <option v-for="f in (editDialog.fam14List || [])" :key="f.base_id" :value="f.base_id">{{ f.name }} ({{ f.base_id }})</option>',
        '          </select>',
        '          <input v-if="!(editDialog.fam14List || []).some(f => f.base_id === editDialog.fam14BaseId)" class="input mono" v-model="editDialog.fam14BaseId" placeholder="z.B. FF800080" style="text-transform: uppercase; max-width: 10rem">',
        '        </div>',
        '        <label>Bus-Adresse</label>',
        '        <div style="display: flex; gap: 0.3rem; align-items: center">',
        '          <input class="input" type="number" min="1" max="99" v-model="editDialog.fam14StartAddress" placeholder="z.B. 2" style="max-width: 5rem">',
        '          <span style="color: var(--muted); font-size: 0.72rem">PCT14-Start-Adresse, dezimal</span>',
        '        </div>',
        '        <span></span>',
        '        <button class="btn btn-secondary btn-sm" @click="regenerateRxIdsFromFam14" style="max-width: 18rem">🔄 Rx-IDs berechnen (Vorschau)</button>',
        '      </div>',
        '      <div v-if="editDialog.rxPreview && editDialog.rxPreview.length" style="margin-top: 0.5rem; font-size: 0.72rem">',
        '        <b>Vorschau:</b>',
        '        <div style="display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.2rem">',
        '          <span v-for="p in editDialog.rxPreview" :key="p.address" class="tag mono">Adr {{ p.address }} → {{ p.sender_id }}</span>',
        '        </div>',
        '        <div style="margin-top: 0.5rem">',
        '          <button class="btn btn-primary btn-sm" @click="applyRxIdsFromPreview">✓ In Channels übernehmen</button>',
        '          <span style="margin-left: 0.5rem; color: var(--muted); font-size: 0.7rem">Überschreibt die EnOcean-IDs der Channels. Name, Sender, Beobachter bleiben.</span>',
        '        </div>',
        '      </div>',
        '      <div v-if="(editDialog.fam14List || []).length === 0" style="margin-top: 0.4rem; font-size: 0.7rem; color: var(--muted); font-style: italic">Tipp: Trage deine FAM14-Module einmalig im <b>🔢 ID-Rechner</b> ein, dann erscheinen sie hier als Dropdown.</div>',
        '    </div>',
        '    <div v-else>',
        '      <div style="background: var(--tint); padding: 0.7rem; border-radius: var(--radius-sm); margin-bottom: 0.8rem">',
        '        <label style="font-weight: 500; display: block; margin-bottom: 0.3rem">🔍 Modell suchen</label>',
        '        <div style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.4rem">Hersteller + Modell tippen — Channels werden anhand des Geräte-Typs aus der Datenbank automatisch erzeugt.</div>',
        '        <input class="input" v-model="editDialog.modelSearch" @input="lookupModels" placeholder="z.B. FDG14, FSR14, FWS61 …">',
        '        <div v-if="editDialog.modelSuggestions.length" style="background: white; border: 1px solid var(--border); border-radius: var(--radius-sm); margin-top: 0.3rem; max-height: 12rem; overflow-y: auto">',
        '          <div v-for="p in editDialog.modelSuggestions" :key="p.manufacturer+p.model" @click="selectProduct(p)" style="padding: 0.4rem 0.6rem; cursor: pointer; border-bottom: 1px solid var(--border)" onmouseover="this.style.background=\'var(--tint)\'" onmouseout="this.style.background=\'white\'">',
        '            <div><b>{{ p.manufacturer }}</b> · {{ p.model }} <span style="color: var(--muted); font-size: 0.78rem">· {{ p.eep || "?" }} · {{ p.channel_count }} {{ p.channel_count === 1 ? "Kanal" : "Kanäle" }}</span></div>',
        '            <div v-if="p.description" style="font-size: 0.72rem; color: var(--muted)">{{ p.description }}</div>',
        '          </div>',
        '        </div>',
        '        <div v-if="editDialog.selectedProduct" style="margin-top: 0.4rem; padding: 0.3rem 0.5rem; background: white; border-radius: var(--radius-sm); font-size: 0.78rem">',
        '          ✓ <b>{{ editDialog.selectedProduct.manufacturer }} {{ editDialog.selectedProduct.model }}</b> ausgewählt — {{ editDialog.selectedProduct.channel_count }} Kanäle vorbefüllt mit EEP <span class="mono">{{ editDialog.selectedProduct.eep }}</span>',
        '          <button class="btn btn-ghost btn-sm" @click="clearProductSelection" style="margin-left: 0.4rem; padding: 0.1rem 0.4rem">×</button>',
        '        </div>',
        // M72: FAM14-Bus-Anbindung. Nur bei product.fam14_bus = true sichtbar.
        // User wählt FAM14-Base (aus localStorage des ID-Rechners) + Bus-Adresse,
        // System generiert automatisch alle RX-IDs als Base | (Adr HEX).
        '        <div v-if="editDialog.fam14Bus" style="margin-top: 0.5rem; padding: 0.5rem 0.7rem; background: rgba(64,224,208,0.08); border-left: 3px solid var(--mint); border-radius: var(--radius-sm); font-size: 0.78rem">',
        '          <div style="margin-bottom: 0.4rem"><b>🔌 FAM14-Bus-Anbindung</b> — Dieses Modul vergibt seine RX-Sender-IDs aus dem FAM14-Block. Trage Base-ID und PCT14-Bus-Start-Adresse ein, die IDs werden automatisch generiert (Adresse als Hex direkt ins letzte Byte).</div>',
        '          <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.4rem 0.6rem; align-items: center">',
        '            <label>FAM14</label>',
        '            <div style="display: flex; gap: 0.3rem; align-items: center">',
        '              <select class="input" v-model="editDialog.fam14BaseId" @change="rescaffoldChannels" style="max-width: 18rem">',
        '                <option value="">— manuell eintragen —</option>',
        '                <option v-for="f in (editDialog.fam14List || [])" :key="f.base_id" :value="f.base_id">{{ f.name }} ({{ f.base_id }})</option>',
        '              </select>',
        '              <input v-if="!(editDialog.fam14List || []).some(f => f.base_id === editDialog.fam14BaseId)" class="input mono" v-model="editDialog.fam14BaseId" @change="rescaffoldChannels" placeholder="z.B. FF800080" style="text-transform: uppercase; max-width: 10rem">',
        '            </div>',
        '            <label>Bus-Adresse</label>',
        '            <div style="display: flex; gap: 0.3rem; align-items: center">',
        '              <input class="input" type="number" min="1" max="99" v-model="editDialog.fam14StartAddress" @change="rescaffoldChannels" placeholder="z.B. 2" style="max-width: 5rem">',
        '              <span style="color: var(--muted); font-size: 0.72rem">PCT14-Start-Adresse, dezimal · Modul belegt {{ editDialog.selectedProduct && editDialog.selectedProduct.channel_count || 1 }} aufeinanderfolgende Adresse(n)</span>',
        '            </div>',
        '          </div>',
        '          <div v-if="editDialog.rxPreview && editDialog.rxPreview.length" style="margin-top: 0.5rem; font-size: 0.72rem">',
        '            <b>Auto-Vorschau Rx-IDs:</b>',
        '            <div style="display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.2rem">',
        '              <span v-for="p in editDialog.rxPreview" :key="p.address" class="tag mono">Adr {{ p.address }} → {{ p.sender_id }}</span>',
        '            </div>',
        '          </div>',
        '          <div v-if="(editDialog.fam14List || []).length === 0" style="margin-top: 0.4rem; font-size: 0.7rem; color: var(--muted); font-style: italic">Tipp: Trage deine FAM14-Module einmalig im <b>🔢 ID-Rechner</b> ein, dann erscheinen sie hier als Dropdown.</div>',
        '        </div>',
        // FTS14EM-Drehschalter-Konfig (Add-Modus) — nur sichtbar wenn FTS14EM gewaehlt
        // Wichtig: mode==='add' explizit, sonst rendert dieser Block im Edit-Modus
        // zusaetzlich zum Edit-FTS14EM-Block weiter oben (Doppelung).
        '        <div v-if="editDialog.mode === \'add\' && editDialog.isFts14em" style="margin-top: 0.5rem; padding: 0.5rem 0.7rem; background: rgba(255,165,0,0.08); border-left: 3px solid #ffa500; border-radius: var(--radius-sm); font-size: 0.78rem">',
        '          <div style="margin-bottom: 0.4rem"><b>🎛 FTS14EM-Drehschalter</b> — Sender-IDs werden aus Gruppe + oberem Drehschalter generiert (Quasi-Dezimal-Schema, Datenblatt 30 014 060-1). Die IDs laufen ueber RS485, <b>nicht ueber Funk</b> (ohne FTS14FA). Du kannst sie aber als Beobachter an Aktor-Channels haengen.</div>',
        '          <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.4rem 0.6rem; align-items: center">',
        '            <label>Gruppe</label>',
        '            <select class="input" v-model.number="editDialog.fts14emGroup" @change="rescaffoldChannels" style="max-width: 12rem">',
        '              <option :value="1">1 — Taster 1..100</option>',
        '              <option :value="101">101 — Taster 101..200</option>',
        '              <option :value="201">201 — Taster 201..300</option>',
        '              <option :value="301">301 — Taster 301..400</option>',
        '              <option :value="401">401 — Taster 401..500</option>',
        '            </select>',
        '            <label>Oberer Drehschalter</label>',
        '            <select class="input" v-model.number="editDialog.fts14emSubdialPos" @change="rescaffoldChannels" style="max-width: 12rem">',
        '              <option v-for="p in [0,10,20,30,40,50,60,70,80,90]" :key="p" :value="p">{{ p }}</option>',
        '            </select>',
        '            <label>Modus</label>',
        '            <select class="input" v-model="editDialog.fts14emMode" @change="rescaffoldChannels" style="max-width: 18rem">',
        '              <option value="UT">UT — Universaltaster (10 Channels, E1..E10)</option>',
        '              <option value="RT">RT — Richtungstaster (5 gepaarte Wippen)</option>',
        '            </select>',
        '          </div>',
        '          <div v-if="editDialog.rxPreview && editDialog.rxPreview.length" style="margin-top: 0.5rem; font-size: 0.72rem">',
        '            <b>Auto-Vorschau Sender-IDs:</b>',
        '            <div style="display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.2rem">',
        '              <span v-for="p in editDialog.rxPreview" :key="p.input" class="tag mono">E{{ p.input }} → {{ p.sender_id }}</span>',
        '            </div>',
        '          </div>',
        '        </div>',
        '      </div>',
        '      <div style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.6rem 0.8rem; align-items: center; margin-bottom: 0.8rem">',
        '        <label>Device-ID</label>',
        '        <input class="input" v-model="editDialog.form.device_id" placeholder="autom. aus Name wenn leer">',
        '      </div>',
        '    </div>',
        '    <div style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.6rem 0.8rem; align-items: center">',
        '      <label>Name</label>',
        '      <input class="input" v-model="editDialog.form.name" placeholder="Anzeigename">',
        '      <label>Hersteller</label>',
        '      <input class="input" v-model="editDialog.form.manufacturer" placeholder="z.B. Eltako">',
        '      <label>Modell</label>',
        '      <input class="input" v-model="editDialog.form.model" placeholder="z.B. FSR14-4x">',
        '      <label>Raum</label>',
        '      <input class="input" v-model="editDialog.form.room" placeholder="z.B. Wohnzimmer">',
        '      <label>Etage</label>',
        '      <input class="input" v-model="editDialog.form.floor" placeholder="z.B. EG">',
        '      <label style="align-self:flex-start; padding-top:0.4rem">Tags</label>',
        '      <div>',
        '        <div style="display:flex; flex-wrap:wrap; gap:0.3rem; margin-bottom:0.4rem; min-height:1.6rem">',
        '          <span v-for="(t, idx) in editDialog.tags" :key="idx" class="tag" style="cursor:default">',
        '            {{ t }} <span style="margin-left:0.3rem; cursor:pointer; color:var(--muted)" @click="removeTag(idx)">×</span>',
        '          </span>',
        '          <span v-if="!editDialog.tags.length" style="color:var(--muted); font-size:0.8rem; padding:0.2rem 0">keine Tags</span>',
        '        </div>',
        '        <input class="input" v-model="editDialog.tagInput" @keydown="onTagInputKey" placeholder="Tag eingeben + Enter">',
        '        <div v-if="tagSuggestions.length" style="margin-top:0.3rem; display:flex; flex-wrap:wrap; gap:0.3rem">',
        '          <span v-for="s in tagSuggestions" :key="s" class="tag" style="cursor:pointer; opacity:0.7" @click="addTag(s)">+ {{ s }}</span>',
        '        </div>',
        '      </div>',
        '      <label style="align-self:flex-start; padding-top:0.4rem">Notizen</label>',
        '      <textarea class="input" v-model="editDialog.form.notes" rows="2" placeholder="Optionale Notizen"></textarea>',
        '    </div>',
        '    <div v-if="editDialog.channels.length" style="margin-top: 1.5rem">',
        '      <h3 style="font-size: 0.95rem; margin: 0 0 0.5rem 0">Channels</h3>',
        '      <div v-if="editDialog.mode === \'add\'" style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.5rem">Bei Multi-Wert-Geräten (FWZ14, FWS61, …) wird die Aufteilung in mehrere Topics anhand des EEPs automatisch übernommen.</div>',
        '      <div v-else style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.5rem">Channel-ID und EEP sind read-only (sonst würde sich die Topic-Struktur ändern). Name und IDs können angepasst werden; bei Aktoren zusätzlich Sende-PTM und Beobachter.</div>',
        '      <div v-for="(ch, idx) in editDialog.channels" :key="idx" class="card" style="padding: 0.6rem; margin-bottom: 0.5rem">',
        '        <div style="display: grid; grid-template-columns: 5rem 1fr 7rem auto; gap: 0.4rem 0.6rem; align-items: center">',
        '          <input class="input" v-model="ch.channel_id" placeholder="Ch-ID (1, 2.1, ...)" :readonly="editDialog.mode === \'edit\'">',
        '          <input class="input" v-model="ch.name" placeholder="Name">',
        '          <select v-if="editDialog.mode === \'add\'" class="input" v-model="ch.eep">',
        '            <option v-for="e in availableEeps" :key="e.value" :value="e.value">{{ e.label }}</option>',
        '          </select>',
        '          <select v-else-if="channelIsA5_12_family(ch)" class="input" :value="ch.eep" @change="switchChannelEepInDialog(editDialog.original_id, ch, $event.target.value)" :title="\'Sofort wirksam — kein Speichern nötig. Felder, Einheit und Topic passen sich an.\'">',
        '            <option v-for="e in A5_12_EEP_OPTIONS" :key="e.value" :value="e.value">{{ e.label }}</option>',
        '          </select>',
        '          <input v-else class="input mono" v-model="ch.eep" readonly>',
        '          <button v-if="editDialog.mode === \'add\'" class="btn btn-ghost btn-sm" @click="removeChannelRow(idx)" :disabled="editDialog.channels.length <= 1" title="Channel entfernen">🗑</button>',
        '          <button v-else class="btn btn-ghost btn-sm" @click="deleteChannelInDialog(idx)" title="Channel komplett entfernen (Karteileiche aufräumen)">🗑</button>',
        '        </div>',
        '        <div style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.4rem 0.6rem; align-items: center; margin-top: 0.4rem">',
        '          <label style="font-size: 0.78rem">EnOcean-ID (Rx)</label>',
        '          <input class="input" v-model="ch.enocean_id" placeholder="z.B. 05090001" style="font-family: monospace; text-transform: uppercase">',
        '          <label style="font-size: 0.78rem">Einheit</label>',
        '          <input class="input" v-model="ch.unit_override" :placeholder="\'optional, ueberschreibt Default aus EEP-Profile\'" style="font-family: monospace">',
        '        </div>',
        // PTM-Polung-Override pro Kanal — nur bei Schalt-Kanaelen. Leer = global.
        '        <div v-if="channelKind(ch) === \'switch\'" style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.4rem 0.6rem; align-items: center; margin-top: 0.4rem">',
        '          <label style="font-size: 0.78rem" title="Welche Wippen-Hälfte schaltet EIN? Leer = globale Einstellung (Tab Einstellungen).">PTM-Polung</label>',
        '          <select class="input" v-model="ch.meta.ptm_on_press">',
        '            <option value="">global (laut Einstellungen)</option>',
        '            <option value="I">oben schaltet EIN</option>',
        '            <option value="0">unten schaltet EIN</option>',
        '          </select>',
        '        </div>',
        // M95: Etage/Raum-Override pro Channel — nur bei Multi-Channel-Geraeten
        // (z.B. DALI-Dimmer). Leer = erbt vom Geraet. Treibt App-Gruppierung + Topic.
        '        <div v-if="editDialog.channels.length > 1" style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.4rem 0.6rem; align-items: center; margin-top: 0.4rem">',
        '          <label style="font-size: 0.78rem" :title="\'In welchen Raum schaltet DIESER Kanal? Leer = Standort des Geräts.\'">Etage (Kanal)</label>',
        '          <input class="input" v-model="ch.floor" :placeholder="editDialog.form.floor ? (\'leer = \' + editDialog.form.floor) : \'leer = vom Gerät\'">',
        '          <label style="font-size: 0.78rem">Raum (Kanal)</label>',
        '          <input class="input" v-model="ch.room" :placeholder="editDialog.form.room ? (\'leer = \' + editDialog.form.room) : \'leer = vom Gerät\'">',
        '        </div>',
        // M103: Farbleuchte aus 2 Kanaelen — nur bei Dimmer-Kanaelen (z.B. FDG14).
        '        <div v-if="ch.profile && ch.profile.ui_kind === \'dimmer\'" style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.4rem 0.6rem; align-items: center; margin-top: 0.4rem">',
        '          <label style="font-size: 0.78rem" :title="\'Zwei Kanaele mit GLEICHER Gruppe bilden eine Farbleuchte: einer Helligkeit, einer Farbe. Leer = normaler Dimmer.\'">Farbleuchte-Gruppe</label>',
        '          <input class="input" v-model="ch.light_group" placeholder="leer = eigenständig, z.B. Bad Farblicht">',
        '          <label style="font-size: 0.78rem">Rolle</label>',
        '          <select class="input" v-model="ch.light_role"><option value="">— normaler Dimmer</option><option value="brightness">Helligkeit</option><option value="color">Farbe (Hue)</option></select>',
        '        </div>',
        // M59: Multi-Sender-Block. Pro Aktor-Channel beliebig viele Sender-Bindings.
        // Genau einer ist als aktiv markiert (= TX-Quelle). Alle anderen werden
        // automatisch als Beobachter behandelt (Pipeline updated State auch
        // wenn der Aktor von einer anderen ID geschaltet wird, z.B. einem parallelen System).
        '        <div v-if="channelIsActor(ch)" style="margin-top: 0.7rem; padding: 0.5rem 0.7rem; background: var(--tint); border-radius: var(--radius-sm)">',
        '          <label style="font-size: 0.78rem; display: block; margin-bottom: 0.3rem"><b>📡 Sender (TX)</b> — genau einer ist aktiv für TX. Alle weiteren werden automatisch als Beobachter ausgewertet (Aktor-State synchronisiert sich auch wenn er von woanders geschaltet wird).</label>',
        // M65: Anlern-Prozedur pro Aktor-Modell direkt sichtbar
        '          <div v-if="editDialog.productInfo && editDialog.productInfo.teach_in_procedure" style="margin-bottom: 0.6rem; padding: 0.5rem 0.7rem; background: rgba(64,224,208,0.10); border-left: 3px solid var(--mint); border-radius: var(--radius-sm); font-size: 0.78rem">',
        '            <div style="margin-bottom: 0.25rem"><b>🔧 Anlernen am {{ editDialog.form.manufacturer }} {{ editDialog.form.model }}:</b></div>',
        '            <pre style="margin: 0; font-family: inherit; white-space: pre-wrap; line-height: 1.4">{{ editDialog.productInfo.teach_in_procedure }}</pre>',
        '          </div>',
        // M83: ReMan-Pairing-Block fuer OPUS BRiDGE (D2-01-XX / D2-05-XX).
        '          <div v-if="channelIsReManActor(ch)" style="margin-bottom: 0.7rem; padding: 0.6rem 0.7rem; background: rgba(99,102,241,0.08); border-left: 3px solid #6366f1; border-radius: var(--radius-sm)">',
        '            <div style="font-size: 0.82rem; margin-bottom: 0.4rem"><b>🔗 OPUS-Pairing (Remote Management)</b> — dieser Aktor lernt nur per Security-Code (vom QR-Code des Moduls), nicht klassisch.</div>',
        '            <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.35rem 0.5rem; align-items: center; font-size: 0.8rem">',
        '              <label>Security-Code</label>',
        '              <input class="input mono" v-model="editDialog.pairing.securityCode" placeholder="8 Hex, z.B. 11223344" style="text-transform: uppercase; max-width: 14rem">',
        '              <label>Sende-Gateway</label>',
        '              <select class="input" v-model="editDialog.pairing.gateway" style="max-width: 18rem">',
        '                <option value="">— Gateway wählen (vergibt freie ID) —</option>',
        '                <option v-for="g in (editDialog.gateways || [])" :key="g.name" :value="g.name">{{ g.name }}{{ g.enabled === false ? " (inaktiv)" : "" }}<span v-if="g.base_id"> · {{ g.base_id }}</span></option>',
        '              </select>',
        '              <label>oder feste Sender-ID</label>',
        '              <input class="input mono" v-model="editDialog.pairing.subAddr" placeholder="optional, 8 Hex — sonst nächste freie aus GW" style="text-transform: uppercase; max-width: 14rem">',
        '            </div>',
        '            <div style="margin-top: 0.5rem; display: flex; gap: 0.5rem; align-items: center">',
        '              <button class="btn btn-sm btn-primary" :disabled="editDialog.pairing.busy" @click="startReManPairing(ch)">{{ editDialog.pairing.busy ? "Pairing läuft…" : "🔗 Pairing starten" }}</button>',
        '              <span style="font-size: 0.72rem; color: var(--muted)">Aktor muss empfangsbereit sein (frisch unter Spannung gesetzt)</span>',
        '            </div>',
        '            <div v-if="editDialog.pairing.result" style="margin-top: 0.5rem; font-size: 0.76rem; padding: 0.4rem 0.5rem; background: rgba(0,0,0,0.03); border-radius: var(--radius-sm)">{{ editDialog.pairing.result }}</div>',
        '          </div>',
        '          <div v-for="(s, sidx) in (ch.senders || [])" :key="sidx" style="padding: 0.3rem 0; border-bottom: 1px solid var(--border)">',
        '            <div style="display: grid; grid-template-columns: 2rem 8rem 1fr 9rem auto auto; gap: 0.3rem 0.4rem; align-items: center">',
        '              <input type="radio" :checked="s.active" @change="setActiveSender(ch, sidx)" :title="s.active ? \'Aktiv — wird für TX verwendet\' : \'Klicken um diesen Sender als aktiv zu setzen\'">',
        '              <input class="input mono" v-model="s.sender_id" @input="s.gateway_match = gatewayFuerSender(s.sender_id)" placeholder="z.B. FF810055" style="text-transform: uppercase">',
        '              <select class="input" v-model="s.via_gateway">',
        '                <option value="">— Auto: {{ s.gateway_match || \'kein Match\' }} —</option>',
        '                <option v-for="g in (editDialog.gateways || [])" :key="g.name" :value="g.name">{{ g.name }}{{ g.enabled === false ? " (inaktiv)" : "" }} ({{ g.host }})<span v-if="g.base_id"> · {{ g.base_id }}</span></option>',
        '              </select>',
        '              <input class="input" v-model="s.label" placeholder="Label, z.B. Primary, Failover">',
        '              <button class="btn btn-ghost btn-sm" @click="sendTeachInForSender(editDialog.original_id, ch, s)" :disabled="!s.sender_id" :title="\'4BS-Lerntelegramm an den Aktor senden — vorher Aktor in LRN-Modus.\'">📡</button>',
        '              <button class="btn btn-ghost btn-sm" @click="removeSenderFromChannel(ch, sidx)" title="Sender entfernen">🗑</button>',
        '            </div>',
        // M60: Auto-Match-Info unter der Zeile — abhaengig von s.gateway_match
        '            <div v-if="s.sender_id && s.gateway_match" style="font-size: 0.7rem; color: var(--muted); padding: 0.1rem 0 0 2.4rem">',
        '              → Gateway-Block-Match: <b>{{ s.gateway_match }}</b><span v-if="!s.via_gateway"> (wird automatisch verwendet wenn nichts gewählt)</span>',
        '            </div>',
        '            <div v-else-if="s.sender_id && !s.gateway_match" style="font-size: 0.7rem; color: var(--korall); padding: 0.1rem 0 0 2.4rem">',
        '              ⚠ Sender-ID liegt in keinem bekannten Gateway-Block — Lerntelegramm wird mit falscher ID raussenden.',
        '            </div>',
        '          </div>',
        '          <div v-if="!ch.senders || !ch.senders.length" style="font-style: italic; color: var(--muted); padding: 0.3rem 0; font-size: 0.78rem">Noch kein Sender — der Aktor kann nicht über uns angesteuert werden, bis mindestens einer mit aktiv markiert hinzugefügt ist.</div>',
        '          <div style="display: flex; gap: 0.4rem; flex-wrap: wrap; margin-top: 0.4rem; align-items: center">',
        '            <button class="btn btn-ghost btn-sm" @click="addSenderToChannel(ch)">+ Sender hinzufügen (manuell)</button>',
        '            <span style="font-size: 0.72rem; color: var(--muted)">oder nächste freie ID:</span>',
        '            <button v-for="g in (editDialog.gateways || [])" :key="g.name" class="btn btn-ghost btn-sm" @click="addSenderFromGateway(ch, g.name)" :title="\'Naechste freie ID aus dem Block von \' + g.name + \' nehmen\'" :style="g.enabled === false ? \'opacity:0.6\' : \'\'">+ via {{ g.name }}{{ g.enabled === false ? " (inaktiv)" : "" }}</button>',
        '          </div>',
        '        </div>',
        '        <div v-if="channelIsA5_12_family(ch)" style="margin-top: 0.5rem; padding: 0.5rem 0.7rem; background: rgba(255,165,0,0.08); border-left: 3px solid var(--korall); border-radius: var(--radius-sm); font-size: 0.78rem; color: var(--muted)">',
        '          ℹ <b>Eingangstyp Strom/Gas/Wasser:</b> Am Geraet selbst wird der Eingangstyp mit dem Eltako <b>PCT14</b>-Tool eingestellt. Dieser Channel hier muss dazu passen — entweder oben direkt im EEP-Auswahlmenu setzen, oder bei einem Power-Cycle des Zaehlers wird ein Lerntelegramm gesendet und der EEP synchronisiert sich automatisch. Beim Wechsel passen sich Feld und Einheit an; unit_override wird verworfen. <b>Channel-Name ggf. manuell anpassen</b> (z.B. „Strom Heiz" → „Gas Heiz"), damit der MQTT-Topic-Pfad dazu passt.',
        '        </div>',
        '        <div v-if="channelIsActor(ch)" style="margin-top: 0.5rem">',
        '          <label style="font-size: 0.78rem; display: block; margin-bottom: 0.2rem">👁 Beobachter (Wand-PTMs, Bewegungsmelder, Zeitschalter — schalten den Aktor direkt)</label>',
        '          <div style="display: flex; flex-wrap: wrap; gap: 0.3rem; min-height: 1.4rem; margin-bottom: 0.3rem">',
        '            <span v-for="(obs, oidx) in ch.observers" :key="oidx" class="tag" style="font-family: monospace; display: inline-flex; align-items: center; gap: 0.3rem">',
        '              <span>{{ normalizeObserver(obs).sender_id }}</span>',
        '              <span v-if="normalizeObserver(obs).match !== \'any\'" style="font-family: sans-serif; color: var(--mint); font-size: 0.72rem">· {{ observerMatchLabel(normalizeObserver(obs).match) }}</span>',
        '              <span style="cursor: pointer; color: var(--muted)" @click="removeObserverFromChannel(ch, oidx)">×</span>',
        '            </span>',
        '            <span v-if="!ch.observers.length" style="color: var(--muted); font-size: 0.78rem; padding: 0.2rem 0">noch keine</span>',
        '          </div>',
        '          <div style="display: flex; gap: 0.3rem; flex-wrap: wrap">',
        '            <input class="input" v-model="ch.observerInput" @keydown="onObserverInputKey(ch, $event)" placeholder="8-Hex-ID (z.B. FF810055)" style="font-family: monospace; text-transform: uppercase; max-width: 11rem">',
        '            <select class="input" v-model="ch.observerMatchSel" style="max-width: 14rem" title="Filter: welche Telegramme dieser ID den Aktor steuern sollen">',
        '              <option value="any">Alle (egal welche Taste)</option>',
        '              <option value="rocker:A">RT A — Wippe A (beide Richtungen)</option>',
        '              <option value="rocker:B">RT B — Wippe B (beide Richtungen)</option>',
        '              <option value="event:B_top">UT 1 — rechts oben (0x70)</option>',
        '              <option value="event:B_bottom">UT 2 — rechts unten (0x50)</option>',
        '              <option value="event:A_top">UT 3 — links oben (0x30)</option>',
        '              <option value="event:A_bottom">UT 4 — links unten (0x10)</option>',
        '            </select>',
        '            <button class="btn btn-ghost btn-sm" @click="addObserverToChannel(ch)">+ hinzufügen</button>',
        '          </div>',
        '        </div>',
        '      </div>',
        '      <button v-if="editDialog.mode === \'add\'" class="btn btn-ghost btn-sm" @click="addChannelRow">+ Channel</button>',
        '    </div>',
        '    <div style="margin-top: 1.5rem; text-align: right">',
        '      <button class="btn btn-ghost" @click="closeEdit">Abbrechen</button>',
        '      <button class="btn btn-primary" :disabled="editDialog.saving" @click="saveEdit">{{ editDialog.saving ? "Speichert …" : "Speichern" }}</button>',
        '    </div>',
        '  </div>',
        '</div>',
    ].join("\n"),
};

const TabGateways = {
    setup() {
        const gws = ref([]);
        let timer = null;

        const dialog = reactive({
            open: false,
            saving: false,
            mode: "edit",  // "edit" oder "add"
            originalName: "",
            form: {
                name: "", type: "TCM310-LAN", host: "", port: 5000,
                enabled: true, rssi_filter: null, repeater_level: 0,
                floor_assignments: [],
            },
            floorInput: "",
        });

        async function refresh() {
            try { gws.value = await api.get("/api/gateways"); } catch (e) { /* silent */ }
        }
        function openAdd() {
            dialog.open = true;
            dialog.mode = "add";
            dialog.saving = false;
            dialog.originalName = "";
            dialog.form = {
                name: "", type: "TCM310-LAN", host: "", port: 5000,
                enabled: true, base_id: "",  // M61b: base_id editierbar
                rssi_filter: null, repeater_level: 0,
                floor_assignments: [],
            };
            dialog.floorInput = "";
        }
        function openEdit(gw) {
            dialog.open = true;
            dialog.mode = "edit";
            dialog.saving = false;
            dialog.originalName = gw.name;
            dialog.form = {
                name: gw.name,
                type: gw.type || "TCM310-LAN",
                host: gw.host,
                port: gw.port,
                enabled: !!gw.enabled,
                base_id: gw.base_id || "",
                rssi_filter: gw.rssi_filter,
                repeater_level: gw.repeater_level || 0,
                floor_assignments: (gw.floor_assignments || []).slice(),
            };
            dialog.floorInput = "";
        }
        function closeDialog() { dialog.open = false; }
        function addFloorTag() {
            const t = dialog.floorInput.trim();
            if (!t || dialog.form.floor_assignments.includes(t)) {
                dialog.floorInput = "";
                return;
            }
            dialog.form.floor_assignments.push(t);
            dialog.floorInput = "";
        }
        function removeFloorTag(idx) {
            dialog.form.floor_assignments.splice(idx, 1);
        }
        function onFloorInputKey(ev) {
            if (ev.key === "Enter" || ev.key === ",") {
                ev.preventDefault();
                addFloorTag();
            }
        }
        async function saveDialog() {
            const payload = Object.assign({}, dialog.form);
            // Port als Zahl
            payload.port = parseInt(payload.port) || 5000;
            if (payload.rssi_filter === "" || payload.rssi_filter == null) {
                payload.rssi_filter = null;
            } else {
                payload.rssi_filter = parseInt(payload.rssi_filter);
            }
            payload.repeater_level = parseInt(payload.repeater_level) || 0;
            // M61b: Base-ID validieren — leer ist OK (wird beim Connect ausgelesen)
            const b = (payload.base_id || "").trim().toUpperCase().replace(/\s/g, "");
            if (b) {
                if (!/^[0-9A-F]{8}$/.test(b)) {
                    toast("Base-ID muss 8 Hex-Zeichen sein (z.B. FF810000), oder leer lassen", "error");
                    dialog.saving = false;
                    return;
                }
                payload.base_id = b;
            } else {
                payload.base_id = null;
            }
            dialog.saving = true;
            try {
                if (dialog.mode === "add") {
                    await api.post("/api/gateways", payload);
                    toast("Gateway angelegt — Container neu starten", "success");
                } else {
                    await api.put("/api/gateways/" + encodeURIComponent(dialog.originalName), payload);
                    toast("Gateway gespeichert — Container neu starten fuer Netzwerk-Aenderungen", "success");
                }
                closeDialog();
                refresh();
            } catch (e) {
                toast("Fehler: " + e.message, "error");
            } finally {
                dialog.saving = false;
            }
        }
        async function deleteGw(name) {
            if (!confirm('Gateway "' + name + '" wirklich entfernen?')) return;
            try {
                await api.del("/api/gateways/" + encodeURIComponent(name));
                toast("Gateway entfernt — Container neu starten", "success");
                refresh();
            } catch (e) { toast("Loeschen fehlgeschlagen: " + e.message, "error"); }
        }

        async function copyText(text) {
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                    toast("Kopiert: " + text, "success");
                    return;
                }
            } catch (e) { /* fall through */ }
            try {
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.left = "-9999px";
                document.body.appendChild(ta);
                ta.focus(); ta.select();
                document.execCommand("copy");
                document.body.removeChild(ta);
                toast("Kopiert: " + text, "success");
            } catch (e) { toast("Kopieren fehlgeschlagen", "error"); }
        }
        async function forgetObserved(gatewayName, senderId) {
            if (!window.confirm("Beobachtete ID " + senderId + " entfernen?\n\n"
                + "Sie wird wieder als freie ID vorgeschlagen. Falls die ID "
                + "weiter live gesendet wird, taucht sie beim nächsten Empfang "
                + "automatisch wieder auf.")) return;
            try {
                await api.post(
                    "/api/gateways/" + encodeURIComponent(gatewayName)
                    + "/forget-observed/" + encodeURIComponent(senderId), {}
                );
                toast("Beobachtung " + senderId + " entfernt", "success");
                refresh();
            } catch (e) { toast("Fehler: " + e.message, "error"); }
        }

        onMounted(() => { refresh(); timer = setInterval(refresh, 3000); });
        onUnmounted(() => clearInterval(timer));

        return {
            gws, gwStatus, timeStr,
            dialog, openAdd, openEdit, closeDialog, saveDialog, deleteGw,
            addFloorTag, removeFloorTag, onFloorInputKey,
            copyText, forgetObserved,
        };
    },
    template: [
        '<div class="page-header"><div>',
        '  <h1>Gateways</h1>',
        '  <div class="subtitle">EnOcean LAN-Gateways die wir verwenden</div>',
        '</div>',
        '<div><button class="btn btn-primary" @click="openAdd">+ Gateway hinzufügen</button></div>',
        '</div>',
        '<div v-if="gws.length === 0" class="card">',
        '  <div class="empty-state"><div class="icon">📡</div>',
        '    <p>Keine Gateways konfiguriert. Klicke „+ Gateway hinzufügen".</p>',
        '  </div>',
        '</div>',
        '<div v-for="gw in gws" :key="gw.name" class="card">',
        '  <div style="display: flex; justify-content: space-between; align-items: baseline">',
        '    <div>',
        '      <h3 style="display: flex; align-items: center; gap: 0.6rem">{{ gw.name }}',
        '        <span :class="[\'status-pill\', \'status-\' + gwStatus(gw)]">{{ (gw.runtime && gw.runtime.status) || "disconnected" }}</span>',
        '      </h3>',
        '      <div style="color: var(--muted)">{{ gw.type }} · {{ gw.host }}:{{ gw.port }}</div>',
        '    </div>',
        '    <div style="text-align: right">',
        '      <div style="font-size: 0.85rem; color: var(--muted); margin-bottom: 0.4rem">Base-ID: <span class="mono">{{ gw.base_id || "—" }}</span></div>',
        '      <button class="btn btn-ghost btn-sm" @click="openEdit(gw)">✏️ Bearbeiten</button>',
        '      <button class="btn btn-ghost btn-sm" @click="deleteGw(gw.name)">🗑</button>',
        '    </div>',
        '  </div>',
        '  <div class="cards-grid" style="margin-top: 1rem">',
        '    <div class="stat-card"><div class="label">Empfangen</div><div class="value">{{ (gw.runtime && gw.runtime.received) || 0 }}</div></div>',
        '    <div class="stat-card"><div class="label">Gesendet</div><div class="value">{{ (gw.runtime && gw.runtime.sent) || 0 }}</div></div>',
        '    <div class="stat-card"><div class="label">Verbindungen</div><div class="value">{{ (gw.runtime && gw.runtime.connections) || 0 }}</div></div>',
        '  </div>',
        '  <div v-if="gw.floor_assignments && gw.floor_assignments.length">',
        '    <label>Tag-Zuordnung</label>',
        '    <span v-for="t in gw.floor_assignments" :key="t" class="tag">{{ t }}</span>',
        '  </div>',
        // M60: Block-Belegung — pro Gateway zeigen welche Sender-IDs aus dem
        // 128er-Block bereits in Aktor-Channels verwendet werden, plus die
        // ersten freien IDs als Vorschlag fuer neue Anlern-Aktionen.
        '  <details v-if="gw.block" style="margin-top: 0.8rem; padding: 0.5rem 0.7rem; background: var(--tint); border-radius: var(--radius-sm)">',
        '    <summary style="cursor: pointer; font-size: 0.82rem"><b>📋 Sender-ID-Block</b> · belegt {{ gw.block.used_total }}/{{ gw.block.total }} ({{ gw.block.assigned.length }} zugewiesen · {{ gw.block.observed.length }} im Live-Log beobachtet) · {{ gw.block.free_count }} frei</summary>',
        '    <div style="margin-top: 0.5rem">',
        // Zugewiesen
        '      <div v-if="gw.block.assigned.length" style="margin-bottom: 0.6rem">',
        '        <div style="font-size: 0.75rem; color: var(--muted); margin-bottom: 0.3rem"><b>✓ Zugewiesen</b> — IDs die einem Aktor-Channel als Sender hinterlegt sind:</div>',
        '        <div v-for="a in gw.block.assigned" :key="a.sender_id" style="display: grid; grid-template-columns: 8rem 1fr; gap: 0.3rem 0.8rem; font-size: 0.78rem; padding: 0.15rem 0">',
        '          <span class="mono">{{ a.sender_id }}</span>',
        '          <span>',
        '            <span v-for="(use, uidx) in a.usages" :key="uidx" style="margin-right: 0.5rem">',
        '              <b>{{ use.device_name }}</b> · {{ use.channel_name }}<span v-if="use.label"> · „{{ use.label }}"</span><span v-if="use.active" style="color: var(--mint)"> ● aktiv</span><span v-else style="color: var(--muted)"> ◯ Beobachter</span>',
        '            </span>',
        '          </span>',
        '        </div>',
        '      </div>',
        // Observed (M61)
        '      <div v-if="gw.block.observed.length" style="margin-bottom: 0.6rem">',
        '        <div style="font-size: 0.75rem; color: var(--muted); margin-bottom: 0.3rem"><b>🎧 Beobachtet</b> — im Live-Log empfangene IDs ohne Channel-Zuordnung (gelten als belegt — Aktor irgendwo angelernt):</div>',
        '        <div v-for="o in gw.block.observed" :key="o.sender_id" style="display: grid; grid-template-columns: 8rem 1fr auto; gap: 0.3rem 0.8rem; font-size: 0.78rem; padding: 0.15rem 0">',
        '          <span class="mono">{{ o.sender_id }}</span>',
        '          <span style="color: var(--muted)">{{ o.rorg }} · {{ o.count }}× empfangen · zuletzt {{ o.rssi_dbm }} dBm</span>',
        '          <button class="btn btn-ghost btn-sm" @click="forgetObserved(gw.name, o.sender_id)" title="ID aus Beobacht-Liste entfernen (gibt sie als frei wieder)">🗑</button>',
        '        </div>',
        '      </div>',
        // Freie
        '      <div v-if="gw.block.free_first && gw.block.free_first.length" style="font-size: 0.78rem">',
        '        <span style="color: var(--muted)"><b>Freie IDs</b> (Vorschlag, Klick kopiert):</span>',
        '        <span v-for="f in gw.block.free_first" :key="f" class="tag mono" style="margin-left: 0.3rem; cursor: copy" @click="copyText(f)">{{ f }}</span>',
        '      </div>',
        '    </div>',
        '  </details>',
        '  <div v-if="gw.runtime && gw.runtime.last_error" style="margin-top: 0.5rem; color: var(--coral)">Fehler: {{ gw.runtime.last_error }}</div>',
        '</div>',
        '<div v-if="dialog.open" class="modal-backdrop" @mousedown.self="closeDialog">',
        '  <div class="modal" style="max-width: 32rem">',
        '    <div class="modal-header">',
        '      <h2>{{ dialog.mode === "add" ? "Gateway hinzufügen" : "Gateway bearbeiten" }}</h2>',
        '      <span class="modal-close" @click="closeDialog">×</span>',
        '    </div>',
        '    <p style="font-size: 0.8rem; color: var(--muted)">Änderungen wirken erst nach <b>Container-Restart</b> in Portainer (Netzwerk-Verbindungen werden beim Start aufgebaut).</p>',
        '    <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.6rem 0.8rem; align-items: center">',
        '      <label>Name</label>',
        '      <input class="input" v-model="dialog.form.name" placeholder="z.B. LAN Gateway 1">',
        '      <label>Typ</label>',
        '      <select class="input" v-model="dialog.form.type">',
        '        <option value="TCM310-LAN">TCM310-LAN</option>',
        '        <option value="TCM515-LAN">TCM515-LAN</option>',
        '      </select>',
        '      <label>IP-Adresse</label>',
        '      <input class="input" v-model="dialog.form.host" placeholder="192.168.1.219">',
        '      <label>Port</label>',
        '      <input class="input" type="number" v-model="dialog.form.port" placeholder="5000">',
        '      <label>Aktiviert</label>',
        '      <label><input type="checkbox" v-model="dialog.form.enabled"> Empfang/Senden für diesen GW aktiv</label>',
        // M61b: Base-ID editierbar — entscheidend fuer die Auto-Gateway-Zuordnung
        '      <label>Base-ID</label>',
        '      <div>',
        '        <input class="input mono" v-model="dialog.form.base_id" placeholder="leer = beim Connect auslesen (z.B. FF810000)" style="text-transform: uppercase">',
        '        <div style="font-size:0.72rem; color:var(--muted); margin-top:0.2rem">4-Byte-Hex, durch 0x80 teilbar (z.B. <span class="mono">FF810000</span> oder <span class="mono">FFBD0080</span>). Bestimmt den 128er-Block der Sender-IDs die über dieses Gateway gesendet werden können. Leer = beim Connect automatisch ausgelesen.</div>',
        '      </div>',
        '      <label>RSSI-Filter</label>',
        '      <input class="input" type="number" v-model="dialog.form.rssi_filter" placeholder="Optional, z.B. -85">',
        '      <label>Repeater-Level</label>',
        '      <select class="input" v-model="dialog.form.repeater_level">',
        '        <option :value="0">0 (aus)</option>',
        '        <option :value="1">1</option>',
        '        <option :value="2">2</option>',
        '      </select>',
        '      <label style="align-self:flex-start; padding-top:0.4rem">Etagen/Zonen</label>',
        '      <div>',
        '        <div style="display:flex; flex-wrap:wrap; gap:0.3rem; margin-bottom:0.4rem; min-height:1.6rem">',
        '          <span v-for="(t, idx) in dialog.form.floor_assignments" :key="idx" class="tag">',
        '            {{ t }} <span style="margin-left:0.3rem; cursor:pointer" @click="removeFloorTag(idx)">×</span>',
        '          </span>',
        '          <span v-if="!dialog.form.floor_assignments.length" style="color:var(--muted); font-size:0.8rem; padding:0.2rem 0">keine</span>',
        '        </div>',
        '        <input class="input" v-model="dialog.floorInput" @keydown="onFloorInputKey" placeholder="Tag + Enter (z.B. Erdgeschoss)">',
        '      </div>',
        '    </div>',
        '    <div style="margin-top: 1.5rem; text-align: right">',
        '      <button class="btn btn-ghost" @click="closeDialog">Abbrechen</button>',
        '      <button class="btn btn-primary" :disabled="dialog.saving || !dialog.form.name || !dialog.form.host" @click="saveDialog">{{ dialog.saving ? "Speichert …" : "Speichern" }}</button>',
        '    </div>',
        '  </div>',
        '</div>',
    ].join("\n"),
};

const TabLive = {
    setup() {
        const log = ref([]);
        const filter = ref("");
        // M81: Forensik-Filter — nur Telegramme von Sender-IDs zeigen die
        // KEINEM bekannten Channel zugeordnet sind. Praktisch fuer das
        // Mitschneiden eines OPUS-Bridge-Pairings oder anderer unbekannter
        // Quellen. Persistent in localStorage.
        const onlyUnknown = ref(localStorage.getItem("live_only_unknown") === "1");
        // M81b: zeigt auch Non-RADIO_ERP1 ESP3-Pakete (ReMan, SmartAck etc.)
        // im Live-Log. Standardmaessig AN, weil sonst Pairing-Telegramme
        // verloren gehen. User kann es ausschalten wenn unnoetig.
        const showRaw = ref(localStorage.getItem("live_show_raw") !== "0");
        // M86: Roh-Funk-Modus — zeigt NUR die ungefilterten ESP3-Pakete
        // (jedes Paket vor Cascade/Dedup, mit data+optional Bytes). Default aus.
        const rawMonitor = ref(localStorage.getItem("live_raw_monitor") === "1");
        let ws = null;

        // Dedup-Key fuer Live-Log: Timestamp + Sender + Payload-Hex eindeutig
        // identifiziert ein Telegramm. Verhindert Doppel-Einträge wenn
        // loadRecent() nach einem Reconnect parallel zum WebSocket Push laeuft.
        function entryKey(e) {
            return (e.ts || "") + "|" + (e.sender_id || "") + "|" + (e.payload || "");
        }
        function dedupedPush(entry, front=true) {
            const key = entryKey(entry);
            for (const existing of log.value) {
                if (entryKey(existing) === key) return; // schon drin
            }
            if (front) log.value.unshift(entry);
            else log.value.push(entry);
            if (log.value.length > 200) log.value.length = 200;
        }
        async function loadRecent() {
            try {
                const recent = await api.get("/api/log/recent?limit=100");
                // Neueste zuerst — und gegen bestehende Eintraege dedupen
                for (const e of recent.reverse()) dedupedPush(e, false);
            } catch (e) { /* silent */ }
        }
        function connect() {
            const proto = location.protocol === "https:" ? "wss:" : "ws:";
            ws = new WebSocket(proto + "//" + location.host + "/api/ws/telegrams");
            ws.onmessage = (ev) => {
                try {
                    const entry = JSON.parse(ev.data);
                    dedupedPush(entry, true);
                } catch (_e) { /* ignore */ }
            };
            ws.onclose = () => setTimeout(connect, 3000);
        }
        onMounted(() => { loadRecent().then(connect); });
        onUnmounted(() => { if (ws) { ws.onclose = null; ws.close(); } });

        const filtered = computed(() => {
            const q = filter.value.toLowerCase().trim();
            const unknownOnly = onlyUnknown.value;
            const rawVisible = showRaw.value;
            // M86: Roh-Funk-Modus → NUR die raw-monitor Pakete zeigen
            if (rawMonitor.value) {
                return log.value.filter(e => {
                    if (!e.is_raw_monitor) return false;
                    if (!q) return true;
                    const hay = (e.gw + " " + e.rorg + " " + (e.payload || "") + " " + (e.optional || "")).toLowerCase();
                    return hay.includes(q);
                });
            }
            return log.value.filter(e => {
                // M86: raw-monitor-Eintraege nur im Roh-Modus zeigen
                if (e.is_raw_monitor) return false;
                // M84: eigene TX-Aussendungen immer zeigen (auch bei Filtern)
                if (e.is_tx) {
                    if (!q) return true;
                    const hayTx = (e.gw + " " + e.sender_id + " " + e.rorg + " " + (e.tx_label || "")).toLowerCase();
                    return hayTx.includes(q);
                }
                // M81b: Non-RADIO_ERP1 (ReMan, SmartAck etc.) optional ausblenden
                if (!rawVisible && e.is_raw) return false;
                // M81: "nur unbekannte" → Eintrag muss device-Zuordnung fehlen.
                // Raw-Pakete (sender_id=0) gelten auch als unbekannt.
                if (unknownOnly) {
                    if (e.device && e.device.name) return false;
                }
                if (!q) return true;
                const name = (e.device && e.device.name) || "";
                const hay = (e.gw + " " + e.sender_id + " " + e.rorg + " " + name).toLowerCase();
                return hay.includes(q);
            });
        });
        // localStorage-Sync
        function toggleOnlyUnknown(v) {
            onlyUnknown.value = !!v;
            localStorage.setItem("live_only_unknown", v ? "1" : "0");
        }
        function toggleShowRaw(v) {
            showRaw.value = !!v;
            localStorage.setItem("live_show_raw", v ? "1" : "0");
        }
        function toggleRawMonitor(v) {
            rawMonitor.value = !!v;
            localStorage.setItem("live_raw_monitor", v ? "1" : "0");
        }

        // Render alle topic_split_fields des Profiles fuer den Live-Log
        function formatLogDecoded(e) {
            if (!e.device || !e.device.decoded) return null;
            const profile = getProfile(e.device.eep);
            if (!profile || !profile.fields) {
                if (e.device.decoded.raw_hex) return e.device.decoded.raw_hex;
                return null;
            }
            const parts = [];
            for (const f of profile.fields) {
                if (!f.is_topic_split) continue;
                let v = e.device.decoded[f.name];
                if (v === undefined && e.device.decoded.kind === f.name) {
                    v = e.device.decoded.value;
                }
                if (v === undefined || v === null) continue;
                let s = formatFieldValue(f, v);
                if (f.icon) s = f.icon + " " + s;
                parts.push(s);
            }
            return parts.length ? parts.join(" · ") : null;
        }

        // Rohe decoded-Felder als key=value-Liste fuer den Detail-Block
        function formatDecodedRaw(e) {
            const d = e.device && e.device.decoded;
            if (!d) return null;
            const parts = [];
            for (const [k, v] of Object.entries(d)) {
                if (v === null || v === undefined) continue;
                let val = v;
                if (typeof v === "number") val = Number.isInteger(v) ? v : v.toFixed(2);
                if (typeof v === "boolean") val = v ? "true" : "false";
                if (typeof v === "string") val = '"' + v + '"';
                parts.push(k + "=" + val);
            }
            return parts.join("  ");
        }

        // Payload hex in Bytes aufgespalten fuer Lesbarkeit
        function formatPayloadBytes(payload) {
            if (!payload) return "";
            return payload.match(/.{1,2}/g).join(" ").toUpperCase();
        }

        return {
            log, filter, filtered, timeStr, onlyUnknown, toggleOnlyUnknown,
            showRaw, toggleShowRaw, rawMonitor, toggleRawMonitor,
            formatLogDecoded, formatDecodedRaw, formatPayloadBytes,
        };
    },
    template: [
        '<div class="page-header">',
        '  <div><h1>Live-Log</h1><div class="subtitle">{{ log.length }} Telegramme · {{ filtered.length }} sichtbar</div></div>',
        '  <div style="display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; justify-content: flex-end">',
        '    <label style="font-size: 0.82rem; cursor: pointer; display: flex; align-items: center; gap: 0.3rem" title="Nur Telegramme von Sender-IDs zeigen die KEINEM bekannten Channel zugeordnet sind. Praktisch fuer das Sniffen unbekannter Quellen (z.B. Pairing-Vorgaenge).">',
        '      <input type="checkbox" :checked="onlyUnknown" @change="toggleOnlyUnknown($event.target.checked)"> 👁 nur unbekannte',
        '    </label>',
        '    <label style="font-size: 0.82rem; cursor: pointer; display: flex; align-items: center; gap: 0.3rem" title="Auch Non-RADIO_ERP1 ESP3-Pakete zeigen (REMOTE_MAN_COMMAND, SMART_ACK, RADIO_MESSAGE, RESPONSE etc.). Diese sind fuer OPUS-Bridge-Pairing wichtig — sonst sieht man sie nicht.">',
        '      <input type="checkbox" :checked="showRaw" @change="toggleShowRaw($event.target.checked)"> 🌐 alle Pakettypen',
        '    </label>',
        '    <label style="font-size: 0.82rem; cursor: pointer; display: flex; align-items: center; gap: 0.3rem; color: var(--korall)" title="Roh-Funk-Monitor: JEDES ESP3-Paket ungefiltert (vor Dedup), mit kompletten data + optional Bytes. Zeigt wirklich alles was auf dem Funkbus passiert — auch Duplikate vom zweiten Gateway.">',
        '      <input type="checkbox" :checked="rawMonitor" @change="toggleRawMonitor($event.target.checked)"> 🔬 Roh-Funk (alles)',
        '    </label>',
        '    <input class="input" v-model="filter" placeholder="Filter …" style="max-width: 320px">',
        '  </div>',
        '</div>',
        '<div class="card" style="padding: 0">',
        '  <div v-if="filtered.length === 0" class="empty-state"><div class="icon">📭</div><p>Noch keine Telegramme empfangen.</p></div>',
        // M86: Roh-Funk-Monitor — komplette data + optional Bytes pro Paket
        '  <div v-else-if="rawMonitor">',
        '    <div v-for="(e, i) in filtered" :key="\'raw\'+i" class="log-entry mono" style="display: block; padding: 0.3rem 0.75rem; border-bottom: 1px solid var(--border); font-size: 0.78rem">',
        '      <span style="color: var(--muted)">{{ timeStr(e.ts) }}</span>',
        '      <span style="color: var(--muted); margin-left: 0.5rem">{{ e.gw }}</span>',
        '      <span class="tag" style="font-size: 0.68rem; margin-left: 0.5rem">{{ e.rorg }}</span>',
        '      <span style="margin-left: 0.5rem"><b>data:</b> {{ e.payload }}</span>',
        '      <span v-if="e.optional" style="margin-left: 0.5rem; color: var(--muted)"><b>opt:</b> {{ e.optional }}</span>',
        '    </div>',
        '  </div>',
        '  <div v-else>',
        '    <div v-for="(e, i) in filtered" :key="i" class="log-entry" :style="\'display: block; padding: 0.4rem 0.75rem; border-bottom: 1px solid var(--border);\' + (e.is_tx ? \' background: rgba(64,224,208,0.07);\' : \'\')">',
        '      <div style="display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap">',
        '        <span class="log-time mono" style="font-size: 0.82rem">{{ timeStr(e.ts) }}</span>',
        '        <span v-if="e.is_tx" class="tag" style="font-size: 0.68rem; background: var(--mint); color: white">⬆ TX</span>',
        '        <span class="log-gw" style="font-size: 0.78rem; color: var(--muted)">{{ e.gw }}</span>',
        '        <span class="log-rorg tag" style="font-size: 0.72rem">{{ e.rorg }}</span>',
        '        <span class="log-sender mono" style="font-size: 0.82rem">{{ e.sender_id }}</span>',
        '        <span class="log-rssi mono" style="font-size: 0.78rem; color: var(--muted)">{{ e.rssi_dbm == null ? (e.is_tx ? "—" : "?") : e.rssi_dbm + " dBm" }}</span>',
        '        <span class="log-payload mono" style="font-size: 0.78rem; color: var(--muted)">[{{ formatPayloadBytes(e.payload) }}]</span>',
        '        <span v-if="e.is_tx && e.tx_label" style="font-size: 0.74rem; color: var(--muted); margin-left: auto">→ gesendet: {{ e.tx_label }}</span>',
        '        <span v-else-if="e.device" class="log-named" style="margin-left: auto">→ <b>{{ e.device.name }}</b><span v-if="formatLogDecoded(e)" style="color: var(--mint); margin-left:0.4rem">{{ formatLogDecoded(e) }}</span></span>',
        '      </div>',
        '      <div v-if="e.device && formatDecodedRaw(e)" class="mono" style="font-size: 0.72rem; color: var(--muted); margin-top: 0.15rem; padding-left: 1rem">',
        '        {{ formatDecodedRaw(e) }}',
        '      </div>',
        '    </div>',
        '  </div>',
        '</div>',
    ].join("\n"),
};

const TabTeach = {
    setup() {
        const status = ref({ active: false, captured: [], only_lrn: true });
        const onlyLrn = ref(true);
        let timer = null;

        async function refresh() {
            try {
                status.value = await api.get("/api/teach/captured");
                if (!status.value.active) onlyLrn.value = status.value.only_lrn !== false;
            } catch (e) { /* silent */ }
        }
        async function start() {
            await api.post("/api/teach/start", { only_lrn: !!onlyLrn.value });
            const mode = onlyLrn.value ? "strikt (nur LRN)" : "permissiv (jedes Telegramm)";
            toast("Anlern-Modus aktiv — " + mode, "success");
            refresh();
        }
        async function stop() {
            await api.post("/api/teach/stop");
            toast("Anlern-Modus beendet");
            refresh();
        }
        function addAsDevice(c) {
            toast("Geräte-Anlegen-Modal kommt mit nächstem Update", "error");
        }
        function avgRssi(c) {
            if (!c.rssi_history || c.rssi_history.length === 0) return "?";
            const sum = c.rssi_history.reduce((a, b) => a + b, 0);
            return Math.round(sum / c.rssi_history.length);
        }

        onMounted(() => { refresh(); timer = setInterval(refresh, 1500); });
        onUnmounted(() => clearInterval(timer));

        return { status, onlyLrn, start, stop, addAsDevice, avgRssi, timeStr };
    },
    template: [
        '<div class="page-header">',
        '  <div>',
        '    <h1>Anlernen</h1>',
        '    <div class="subtitle">',
        '      <span v-if="status.active" style="color: var(--mint)">● Anlern-Modus aktiv</span>',
        '      <span v-else>Drücke „Start", dann die Lerntaste/den Wippentaster am Gerät</span>',
        '    </div>',
        '  </div>',
        '  <div style="text-align: right">',
        '    <div v-if="!status.active" style="margin-bottom: 0.5rem; font-size: 0.85rem">',
        '      <label style="cursor: pointer"><input type="checkbox" v-model="onlyLrn"> nur LRN-Telegramme akzeptieren</label>',
        '      <div style="color: var(--muted); font-size: 0.72rem; max-width: 22rem; margin-left: 1.4rem">Empfohlen. Bei Sensoren ohne LRN-Taste (Wand-PTM, schon verbaut) deaktivieren — dann zählt das nächste empfangene Telegramm.</div>',
        '    </div>',
        '    <div v-else style="margin-bottom: 0.5rem; font-size: 0.78rem; color: var(--muted)">',
        '      Modus: <b>{{ status.only_lrn ? "nur LRN-Telegramme" : "jedes Telegramm" }}</b>',
        '    </div>',
        '    <button v-if="!status.active" class="btn btn-primary" @click="start">▶ Anlernen starten</button>',
        '    <button v-else class="btn btn-secondary" @click="stop">■ Stoppen</button>',
        '  </div>',
        '</div>',
        '<div class="card" v-if="status.captured.length === 0">',
        '  <div class="empty-state"><div class="icon">📡</div>',
        '    <p v-if="status.active">Warte auf erstes Telegramm von einem unbekannten Sender …</p>',
        '    <p v-else>Noch nichts erfasst. Klick „Anlernen starten".</p>',
        '  </div>',
        '</div>',
        '<div v-else>',
        '  <div v-for="c in status.captured" :key="c.sender_id" class="card">',
        '    <div style="display: flex; justify-content: space-between; align-items: baseline">',
        '      <div>',
        '        <h3>{{ c.sender_id }}</h3>',
        '        <div style="color: var(--muted)">{{ c.rorg }} · {{ c.count }} Telegramme empfangen</div>',
        '      </div>',
        '      <div style="text-align: right">',
        '        <div style="font-family: var(--font-mono)">Ø {{ avgRssi(c) }} dBm</div>',
        '        <div style="color: var(--muted); font-size: 0.85rem">letzt: {{ timeStr(c.last_seen) }}</div>',
        '      </div>',
        '    </div>',
        '    <div style="margin-top: 0.5rem">',
        '      <label>Payloads</label>',
        '      <span v-for="p in c.payload_history" :key="p" class="tag mono">{{ p }}</span>',
        '    </div>',
        '    <div style="margin-top: 0.75rem; text-align: right">',
        '      <button class="btn btn-primary btn-sm" @click="addAsDevice(c)">+ Als Gerät anlegen</button>',
        '    </div>',
        '  </div>',
        '</div>',
    ].join("\n"),
};

// ===== ID-Rechner (M55) =====
// Hilfsseite zum Berechnen von Eltako-Sender-IDs aus FAM14-Base-ID +
// PCT14-Geraeteadresse. Unterstuetzt mehrere FAM14-Module (z.B. Mehrfamilien-
// haus oder grosses Haus mit mehreren Schaltschraenken). Persistenz
// serverseitig im /data-Volume (M91) ueber /api/fam14, geraeteuebergreifend.

const MODULE_TYPES = [
    { value: 1, label: "1 Sender-ID — DSZ14DRS · FWZ14 · FUD61 · FUD14" },
    { value: 2, label: "2 Sender-IDs — FSR14-2x · FSR14SSR" },
    { value: 3, label: "3 Sender-IDs — F3Z14D" },
    { value: 4, label: "4 Sender-IDs — FSR14-4x · FDG14 (Dimmer)" },
];

function _parseAddr(raw, base) {
    if (!raw) return null;
    const s = String(raw).trim();
    let n;
    if (base === "hex") {
        n = parseInt(s.replace(/^0x/i, ""), 16);
    } else {
        n = parseInt(s, 10);
    }
    if (isNaN(n) || n < 1 || n > 127) return null;
    return n;
}

function _normalizeFamId(raw) {
    if (!raw) return null;
    const s = String(raw).trim().toUpperCase().replace(/[\s\-_]/g, "");
    if (!/^[0-9A-F]{8}$/.test(s)) return null;
    return s;
}

function _addOffsetToId(baseHex, offset) {
    // 8-Hex + integer offset -> 8-Hex
    const high = parseInt(baseHex.slice(0, 4), 16);
    const low = parseInt(baseHex.slice(4, 8), 16) + offset;
    const carry = Math.floor(low / 0x10000);
    const lowFinal = low % 0x10000;
    const highFinal = (high + carry) % 0x10000;
    return (
        highFinal.toString(16).toUpperCase().padStart(4, "0")
        + lowFinal.toString(16).toUpperCase().padStart(4, "0")
    );
}

const TabCalculator = {
    setup() {
        const fams = ref([]);
        const newFamName = ref("");
        const newFamBase = ref("");
        const calc = reactive({
            fam14Idx: 0,
            deviceAddr: "",
            addrBase: "dec",
            moduleType: 3,    // Default F3Z14D
        });
        const reverseInput = ref("");

        async function loadFams() {
            // M91: serverseitig statt localStorage (siehe loadFam14List oben)
            fams.value = await loadFam14List();
        }
        async function saveFams() {
            const saved = await saveFam14List(fams.value);
            if (saved) fams.value = saved;
        }
        function addFam() {
            const base = _normalizeFamId(newFamBase.value);
            if (!base) {
                toast("Base-ID muss 8 Hex-Zeichen sein (z.B. FF800080)", "error");
                return;
            }
            // Eltako-Konvention: Base-IDs enden meist auf 0x80 oder 0x00 (jedes
            // FAM14 belegt 128 IDs ab Base). Wir warnen freundlich, blocken aber nicht.
            const lastByte = parseInt(base.slice(6, 8), 16);
            if (lastByte !== 0x80 && lastByte !== 0x00) {
                toast("Hinweis: Base-IDs eines FAM14 enden ueblicherweise auf 0x00 oder 0x80", "");
            }
            const name = (newFamName.value || "").trim() || ("FAM14 " + (fams.value.length + 1));
            fams.value.push({ name: name, base_id: base });
            newFamName.value = "";
            newFamBase.value = "";
            saveFams();
        }
        function removeFam(idx) {
            fams.value.splice(idx, 1);
            if (calc.fam14Idx >= fams.value.length) calc.fam14Idx = Math.max(0, fams.value.length - 1);
            saveFams();
        }

        const calcResults = computed(() => {
            if (!fams.value.length) return { error: "noch_kein_fam" };
            const fam = fams.value[calc.fam14Idx];
            if (!fam) return { error: "kein_fam_gewaehlt" };
            const addr = _parseAddr(calc.deviceAddr, calc.addrBase);
            if (addr === null) {
                if (calc.deviceAddr) return { error: "addr_ungueltig" };
                return { error: "addr_leer" };
            }
            const n = calc.moduleType;
            // Prueft Ueberlauf: Adresse + (n-1) muss <= 127 sein
            if (addr + n - 1 > 127) {
                return { error: "addr_zu_hoch", maxAddr: 127 - (n - 1) };
            }
            const items = [];
            for (let i = 0; i < n; i++) {
                items.push({
                    label: n === 1 ? "" : ((calc.moduleType === 3 ? "Eingang " : "Kanal ") + (i + 1)),
                    sender_id: _addOffsetToId(fam.base_id, addr + i),
                    offset_hex: "0x" + (addr + i).toString(16).toUpperCase().padStart(2, "0"),
                });
            }
            return { items: items, fam: fam, addr: addr, addrHex: "0x" + addr.toString(16).toUpperCase().padStart(2, "0") };
        });

        const reverseResults = computed(() => {
            const inp = _normalizeFamId(reverseInput.value);
            if (!inp) return null;
            const idInt = parseInt(inp.slice(4, 8), 16);
            const matches = [];
            for (const f of fams.value) {
                // Pruefe ob die ersten 4 Hex-Zeichen passen UND der low-part im Range 0..127 ab Base liegt
                if (f.base_id.slice(0, 4) !== inp.slice(0, 4)) continue;
                const baseLow = parseInt(f.base_id.slice(4, 8), 16);
                const diff = idInt - baseLow;
                if (diff < 0 || diff > 127) continue;
                matches.push({
                    fam: f,
                    addr_dec: diff,
                    addr_hex: "0x" + diff.toString(16).toUpperCase().padStart(2, "0"),
                });
            }
            return matches;
        });

        async function copyText(text) {
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                    toast("Kopiert: " + text, "success");
                    return;
                }
            } catch (e) { /* fall through */ }
            try {
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.left = "-9999px";
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                document.execCommand("copy");
                document.body.removeChild(ta);
                toast("Kopiert: " + text, "success");
            } catch (e) {
                toast("Kopieren fehlgeschlagen", "error");
            }
        }

        onMounted(loadFams);

        return {
            fams, newFamName, newFamBase, calc, reverseInput,
            MODULE_TYPES, calcResults, reverseResults,
            addFam, removeFam, copyText,
        };
    },
    template: [
        '<div class="page-header">',
        '  <div>',
        '    <h1>🔢 ID-Rechner</h1>',
        '    <div class="subtitle">Sender-IDs aus FAM14-Base-ID und PCT14-Geräteadresse berechnen — auch für Anlagen mit mehreren FAM14.</div>',
        '  </div>',
        '</div>',

        // FAM14-Liste
        '<div class="card">',
        '  <h3 style="margin-top:0">Deine FAM14-Module</h3>',
        '  <div style="font-size:0.78rem; color:var(--muted); margin-bottom:0.6rem">Die Base-ID steht in PCT14 → Konfigurationsbereich des FAM14 (4 Hex-Bytes, endet meist auf 0x80 oder 0x00). Mehrere FAM14 sind möglich, etwa bei Mehrfamilienhäusern. Die Liste wird zentral gespeichert und steht auf allen Geräten (Desktop, Handy, Tablet) zur Verfügung.</div>',
        '  <div v-if="!fams.length" style="padding:0.6rem; color:var(--muted); font-style:italic">Noch kein FAM14 eingetragen.</div>',
        '  <div v-for="(f, idx) in fams" :key="idx" style="display:flex; align-items:center; gap:0.6rem; padding:0.3rem 0; border-bottom:1px solid var(--border)">',
        '    <span style="flex:0 0 14rem"><b>{{ f.name }}</b></span>',
        '    <span class="mono">{{ f.base_id }}</span>',
        '    <span style="flex:1"></span>',
        '    <button class="btn btn-ghost btn-sm" @click="removeFam(idx)" title="Entfernen">🗑</button>',
        '  </div>',
        '  <div style="display:flex; gap:0.5rem; align-items:end; margin-top:0.8rem; padding-top:0.6rem; border-top:1px dashed var(--border)">',
        '    <div style="flex:0 0 14rem"><label style="font-size:0.78rem; display:block; color:var(--muted)">Name</label><input class="input" v-model="newFamName" placeholder="z.B. Haus 1 EG"></div>',
        '    <div style="flex:0 0 12rem"><label style="font-size:0.78rem; display:block; color:var(--muted)">Base-ID</label><input class="input mono" v-model="newFamBase" placeholder="FF800080" style="text-transform:uppercase"></div>',
        '    <button class="btn btn-primary" @click="addFam">+ FAM14 hinzufügen</button>',
        '  </div>',
        '</div>',

        // Vorwärts-Rechner
        '<div class="card" style="margin-top:1rem">',
        '  <h3 style="margin-top:0">Sender-IDs berechnen</h3>',
        '  <div style="display:grid; grid-template-columns: 14rem 8rem 5rem 1fr; gap:0.5rem 0.8rem; align-items:end">',
        '    <div><label style="font-size:0.78rem; color:var(--muted); display:block">FAM14</label><select class="input" v-model.number="calc.fam14Idx" :disabled="!fams.length"><option v-for="(f, idx) in fams" :key="idx" :value="idx">{{ f.name }} ({{ f.base_id }})</option></select></div>',
        '    <div><label style="font-size:0.78rem; color:var(--muted); display:block">Geräteadresse</label><input class="input mono" v-model="calc.deviceAddr" placeholder="z.B. 35"></div>',
        '    <div><label style="font-size:0.78rem; color:var(--muted); display:block">Format</label><select class="input" v-model="calc.addrBase"><option value="dec">dez</option><option value="hex">hex</option></select></div>',
        '    <div><label style="font-size:0.78rem; color:var(--muted); display:block">Modul-Typ</label><select class="input" v-model.number="calc.moduleType"><option v-for="t in MODULE_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option></select></div>',
        '  </div>',
        '  <div style="margin-top:1rem">',
        '    <template v-if="calcResults.error === \'noch_kein_fam\'"><div style="padding:0.6rem; color:var(--muted); font-style:italic">Trag oben mindestens ein FAM14-Modul ein.</div></template>',
        '    <template v-else-if="calcResults.error === \'addr_leer\'"><div style="padding:0.6rem; color:var(--muted); font-style:italic">Geräteadresse eingeben (1 - 127).</div></template>',
        '    <template v-else-if="calcResults.error === \'addr_ungueltig\'"><div style="padding:0.6rem; color:var(--korall)">Geräteadresse muss eine Zahl 1 - 127 sein ({{ calc.addrBase === "hex" ? "hex" : "dezimal" }}).</div></template>',
        '    <template v-else-if="calcResults.error === \'addr_zu_hoch\'"><div style="padding:0.6rem; color:var(--korall)">Adresse + Modul-Kanäle übersteigt 127. Maximaladresse für diesen Modul-Typ: {{ calcResults.maxAddr }}.</div></template>',
        '    <template v-else>',
        '      <div style="font-size:0.82rem; color:var(--muted); margin-bottom:0.4rem">{{ calcResults.fam.name }} (Base <span class="mono">{{ calcResults.fam.base_id }}</span>) + Adresse {{ calcResults.addr }} ({{ calcResults.addrHex }})</div>',
        '      <table style="width:auto">',
        '        <thead><tr><th></th><th>Sender-ID</th><th style="color:var(--muted); font-weight:normal">Offset</th><th></th></tr></thead>',
        '        <tbody>',
        '          <tr v-for="(it, idx) in calcResults.items" :key="idx">',
        '            <td>{{ it.label || "—" }}</td>',
        '            <td class="mono"><b>{{ it.sender_id }}</b></td>',
        '            <td class="mono" style="color:var(--muted)">{{ it.offset_hex }}</td>',
        '            <td><button class="btn btn-ghost btn-sm" @click="copyText(it.sender_id)" title="In Zwischenablage">📋</button></td>',
        '          </tr>',
        '        </tbody>',
        '      </table>',
        '    </template>',
        '  </div>',
        '</div>',

        // Rückwärts-Suche
        '<div class="card" style="margin-top:1rem">',
        '  <h3 style="margin-top:0">Rückwärts: aus Sender-ID zur Adresse</h3>',
        '  <div class="hinweistext">Eine empfangene Sender-ID hier eintragen — System sagt zu welchem FAM14 sie gehört und welche Geräteadresse + Offset dahinter steckt.</div>',
        '  <input class="input mono" v-model="reverseInput" placeholder="z.B. FF8000A3" style="text-transform:uppercase; max-width:14rem">',
        '  <div style="margin-top:0.8rem">',
        '    <template v-if="!reverseInput"><div style="color:var(--muted); font-style:italic">Sender-ID eintragen …</div></template>',
        '    <template v-else-if="!reverseResults || reverseResults.length === 0"><div style="color:var(--korall)">Diese ID gehört zu keinem deiner FAM14-Module.</div></template>',
        '    <template v-else>',
        '      <div v-for="(m, idx) in reverseResults" :key="idx" style="padding:0.4rem 0; border-bottom:1px solid var(--border)">',
        '        <b>{{ m.fam.name }}</b> · Base <span class="mono">{{ m.fam.base_id }}</span> · Geräteadresse <b>{{ m.addr_dec }}</b> (<span class="mono">{{ m.addr_hex }}</span>)',
        '      </div>',
        '    </template>',
        '  </div>',
        '</div>',
    ].join("\n"),
};

// ===== Einstellungen (MQTT + globale Defaults) =====
// ===== Heizung =====
// Heizzonen mit Zeitplan: Ist-Wert aus einem beliebigen MQTT-Topic, Sollwert
// je Zeitfenster, Ausgabe als A5-10-06 an den Heizregler.

const WOCHENTAGE = [
    { id: "mo", label: "Mo" }, { id: "di", label: "Di" }, { id: "mi", label: "Mi" },
    { id: "do", label: "Do" }, { id: "fr", label: "Fr" }, { id: "sa", label: "Sa" },
    { id: "so", label: "So" },
];

const HEIZ_STATUS = {
    ok:            { text: "regelt",             farbe: "#16a34a" },
    inaktiv:       { text: "abgeschaltet",       farbe: "#94a3b8" },
    teilweise:     { text: "teilweise",          farbe: "#f59e0b" },
    kein_ziel:     { text: "kein Heizkreis",     farbe: "#f59e0b" },
    kein_fuehler:  { text: "kein Raumfühler",    farbe: "#dc2626" },
    kein_sollwert: { text: "kein Sollwert",      farbe: "#f59e0b" },
    kein_istwert:  { text: "kein Ist-Wert",      farbe: "#dc2626" },
    istwert_alt:   { text: "Ist-Wert veraltet",  farbe: "#dc2626" },
    istwert_unplausibel: { text: "Ist-Wert unplausibel", farbe: "#dc2626" },
};

const TabHeizung = {
    setup() {
        const daten = ref({ aktiv: true, takt_s: 20, basis_topic: "heizung",
                            zeitzone: "Europe/Berlin", regleruhr: "",
                            schalter: [], zonen: [], lage: [], istwerte: [] });
        const ziele = ref([]);
        const offen = ref(null);        // Index des aufgeklappten Bereichs
        // Mehrere Raeume duerfen gleichzeitig offen sein — der Anwender will
        // drei nebeneinander vergleichen koennen (23.09.2026).
        const raumOffen = ref({});      // "<zone.id>|<index>" -> true
        const technikOffen = ref(null); // zone.id, dessen Aktor-Abgleich offen ist
        const laedt = ref(false);
        let timer = null;

        // Die regelmaessige Auffrischung darf NUR die Live-Werte anfassen.
        // Wuerde sie die Konfiguration mit ueberschreiben, waere ein gerade
        // angelegter Bereich nach spaetestens 10 Sekunden wieder weg und
        // jeder halb getippte Name mit ihm.
        async function laden(nurLage) {
            try {
                const antwort = await api.get("/api/heizung");
                daten.value.lage = antwort.lage || [];
                daten.value.istwerte = antwort.istwerte || [];
                // Nur den STAND der Schalter mitziehen, nicht die Liste: der
                // Schalter kann von der Visu umgelegt worden sein, aber ein
                // halb getippter Name darf dabei nicht verschwinden.
                (antwort.schalter || []).forEach(s => {
                    const da = (daten.value.schalter || []).find(x => x.id === s.id);
                    if (da) da.an = s.an;
                });
                daten.value.regleruhr = antwort.regleruhr || "";
                if (nurLage) return;
                daten.value.zeitzone = antwort.zeitzone || "Europe/Berlin";
                daten.value.aktiv = antwort.aktiv;
                daten.value.takt_s = antwort.takt_s;
                daten.value.basis_topic = antwort.basis_topic;
                daten.value.schalter = antwort.schalter || [];
                daten.value.zonen = antwort.zonen || [];
            } catch (e) {
                if (nurLage) return;     // beim Takt still bleiben
                toast("Heizung laden fehlgeschlagen: " + e.message, "error");
            }
        }
        async function zieleLaden() {
            try { ziele.value = (await api.get("/api/heizung/ziele")).ziele || []; }
            catch (e) { /* silent */ }
        }

        async function speichern() {
            laedt.value = true;
            try {
                const antwort = await api.put("/api/heizung", {
                    aktiv: daten.value.aktiv,
                    takt_s: daten.value.takt_s,
                    basis_topic: daten.value.basis_topic,
                    zeitzone: daten.value.zeitzone,
                    schalter: daten.value.schalter,
                    zonen: daten.value.zonen,
                });
                daten.value.lage = antwort.lage || [];
                daten.value.schalter = antwort.schalter || [];
                // Die geprueften und vervollstaendigten Daten vom Server
                // zurueckuebernehmen — so sieht man sofort, was wirklich
                // gespeichert wurde.
                daten.value.zonen = antwort.zonen || [];
                toast("Gespeichert", "success");
            } catch (e) {
                toast("Speichern fehlgeschlagen: " + e.message, "error");
            } finally {
                laedt.value = false;
            }
        }

        async function jetztSenden() {
            try {
                const antwort = await api.post("/api/heizung/senden");
                daten.value.lage = antwort.lage || [];
                const n = (antwort.gesendet || []).length;
                toast(n ? ("Gesendet an " + n + " Heizkreis(e): "
                           + antwort.gesendet.join(", "))
                        : "Nichts gesendet — siehe Hinweise bei den Räumen",
                      n ? "success" : "error");
            } catch (e) {
                toast("Fehlgeschlagen: " + e.message, "error");
            }
        }

        function neueZone() {
            const n = daten.value.zonen.length + 1;
            daten.value.zonen.push({
                id: "Bereich_" + n, name: "Bereich " + n, aktiv: true,
                raeume: [], fuehler_regel: "mittelwert",
                offset_grenze_k: 3.0,
                kontakte: [], absenkung_zeitzone: "", absenkung_k: 3.0,
                ist_max_alter_s: 1800, ist_min_c: 0.0, ist_max_c: 40.0,
                basissollwert_c: 21.0, verschiebung_max_k: 5.0,
                sollwert_offset_k: 0.0, sende_intervall_s: 300,
                fenster: [],
                // Zeitzonen nur fuer Zeiten MIT Heizbedarf; alles andere faellt
                // hierauf zurueck.
                restzeit: { aktiv: true, name: "Nachtabsenkung",
                            sollwert_c: 18.0, sollwert_topic: "",
                            sollwert_feld: "" },
            });
            offen.value = daten.value.zonen.length - 1;
        }

        // Aeltere Bereiche haben den Rueckfall-Sollwert noch nicht —
        // das Formular darf daran nicht auflaufen.
        function restzeit(zone) {
            if (!zone.restzeit) {
                zone.restzeit = { aktiv: false, name: "Nachtabsenkung",
                                  sollwert_c: 18.0, sollwert_topic: "",
                                  sollwert_feld: "" };
            }
            return zone.restzeit;
        }

        function neuerSchalter() {
            const n = daten.value.schalter.length + 1;
            daten.value.schalter.push({
                id: "schalter_" + n, name: "Schalter " + n, an: false,
            });
        }
        function schalterLoeschen(idx) {
            const s = daten.value.schalter[idx];
            if (!confirm("Schalter \"" + (s.name || s.id) + "\" entfernen? "
                         + "Zeitzonen und Räume, die auf ihn zeigen, gelten "
                         + "danach als NICHT freigegeben.")) return;
            daten.value.schalter.splice(idx, 1);
        }
        // Umlegen geht sofort an den Server — nicht erst beim Speichern.
        // Sonst legt man den Gäste-Schalter um und nichts passiert.
        async function schalterUmlegen(s) {
            const neu = !s.an;
            try {
                const antwort = await api.post(
                    "/api/heizung/schalter/" + encodeURIComponent(s.id)
                    + "?an=" + (neu ? "true" : "false"));
                daten.value.schalter = antwort.schalter || [];
                daten.value.lage = antwort.lage || [];
                toast((s.name || s.id) + ": " + (neu ? "an" : "aus"), "success");
            } catch (e) {
                toast("Schalter nicht umgelegt: " + e.message
                      + " — noch nicht gespeichert?", "error");
            }
        }
        function zoneLoeschen(idx) {
            const z = daten.value.zonen[idx];
            if (!confirm("Bereich \"" + (z.name || z.id) + "\" wirklich entfernen?")) return;
            daten.value.zonen.splice(idx, 1);
            offen.value = null;
        }
        function neuesFenster(zone) {
            zone.fenster.push({
                name: "Fenster " + (zone.fenster.length + 1),
                von: "06:00", bis: "09:00", tage: [], sollwert_c: 21.0,
                sollwert_topic: "", sollwert_feld: "",
            });
        }
        function fensterLoeschen(zone, idx) { zone.fenster.splice(idx, 1); }

        function tagUmschalten(fenster, tag) {
            const i = fenster.tage.indexOf(tag);
            if (i >= 0) fenster.tage.splice(i, 1); else fenster.tage.push(tag);
        }

        function neuerFuehler(traeger) {
            if (!traeger.fuehler) traeger.fuehler = [];
            traeger.fuehler.push({ topic: "", feld: "", name: "", gewicht: 1.0 });
        }
        function fuehlerLoeschen(traeger, idx) { traeger.fuehler.splice(idx, 1); }

        function neuerKontakt(traeger) {
            if (!traeger.kontakte) traeger.kontakte = [];
            traeger.kontakte.push({
                topic: "", feld: "closed", name: "", bedeutung: "geschlossen",
            });
        }
        // Spiegelt kontakt_ist_offen() aus app/heizung.py — nur für die Anzeige.
        const TEXT_OFFEN = ["open", "opened", "offen", "geöffnet", "auf"];
        const TEXT_ZU = ["closed", "close", "geschlossen", "zu", "shut"];
        function kontaktOffen(k) {
            const e = wert(k.topic, k.feld);
            if (!e) return false;
            const v = e.wert;
            if (typeof v === "string") {
                const t = v.trim().toLowerCase();
                if (TEXT_OFFEN.indexOf(t) >= 0) return true;
                if (TEXT_ZU.indexOf(t) >= 0) return false;
            }
            const wahr = (v === true || v === 1 || v === "1" || v === "true");
            return k.bedeutung === "offen" ? wahr : !wahr;
        }

        // Das Sollwertrad haengt am Raum (0 oder 1).
        function offsetSetzen(raum, feld, wert) {
            if (!raum.offset_quelle) {
                raum.offset_quelle = { topic: "", feld: "", name: "" };
            }
            raum.offset_quelle[feld] = wert;
            // Ganz leer = kein Sollwertrad, damit nichts Halbes gespeichert wird
            const q = raum.offset_quelle;
            if (!q.topic && !q.feld && !q.name) raum.offset_quelle = null;
        }

        function neuerRaum(zone) {
            const n = zone.raeume.length + 1;
            zone.raeume.push({
                id: "raum_" + n, name: "Raum " + n, aktiv: true,
                fuehler: [], fuehler_regel: null,
                offset_quelle: null, sollwert_offset_k: 0.0,
                handverschiebung_k: 0.0,
                kontakte: [], heizkreise: [],
                dauerabsenkung: false, dauerabsenkung_ausnahme_schalter: "",
            });
        }
        function neuerHeizkreis(raum) {
            raum.heizkreise.push({
                device_id: "", channel_id: "", name: "",
                basissollwert_c: null, verschiebung_max_k: null,
            });
        }

        // Raum entfernen. Bewusst mit Rückfrage und in der Kopfzeile des
        // Kästchens: der Knopf stand bisher unten zwischen den Feldern und
        // war nicht zu finden. Die Rückfrage sagt, was
        // mitgeht — und was der Ausgang am Regler danach macht.
        function raumLoeschen(zone, ridx) {
            const raum = zone.raeume[ridx];
            const teile = [];
            if ((raum.fuehler || []).length) teile.push(raum.fuehler.length + " Fühler");
            if ((raum.kontakte || []).length) teile.push(raum.kontakte.length + " Fensterkontakt(e)");
            if ((raum.heizkreise || []).length) teile.push(raum.heizkreise.length + " Heizkreis(e)");
            let text = "Raum \"" + (raum.name || raum.id || "Raum") + "\" entfernen?";
            if (teile.length) text += "\n\nMit ihm verschwinden: " + teile.join(", ") + ".";
            if ((raum.heizkreise || []).length) {
                text += "\n\nDer Ausgang am Regler bekommt danach kein Telegramm "
                    + "mehr und meldet nach 90 Minuten Sensorausfall — er bleibt "
                    + "auf seiner letzten Stellgröße stehen, ist also nicht aus.";
            }
            if (!confirm(text)) return;
            zone.raeume.splice(ridx, 1);
            // Die Aufklapp-Zustände hängen am Index und stimmen danach nicht mehr.
            raumOffen.value = {};
            toast("Raum entfernt. Noch nicht gespeichert — \"Speichern\" nicht "
                  + "vergessen.", "success");
        }
        function raumIstOffen(zone, ridx) {
            return !!raumOffen.value[zone.id + "|" + ridx];
        }
        function raumUmschalten(zone, ridx) {
            const s = zone.id + "|" + ridx;
            if (raumOffen.value[s]) delete raumOffen.value[s];
            else raumOffen.value[s] = true;
        }
        function alleRaeume(zone, auf) {
            zone.raeume.forEach((_, i) => {
                const s = zone.id + "|" + i;
                if (auf) raumOffen.value[s] = true; else delete raumOffen.value[s];
            });
        }

        // Raum in einen anderen Bereich verschieben.
        //
        // Der Heizkreis zieht mit um — und im Regler sind Basissollwert und
        // Verschiebungsbereich JE KANAL eingestellt. Sie
        // gehoeren also zum Ausgang, nicht zum Bereich, und aendern sich
        // beim Umzug nicht. Die Vorgabe des Bereichs ist nur der Default
        // fuer Kreise, die nichts Eigenes eingetragen haben — beim Umzug wird
        // sie deshalb am Kreis festgeschrieben. Sonst uebernaehme der Raum
        // stillschweigend die Werte des neuen Bereichs und die Verschiebung
        // kaeme falsch skaliert am Regler an, ohne Fehlermeldung.
        // Vue 3 wertet v-if VOR v-for aus — die Auswahl der Ziele gehoert
        // deshalb hierher und nicht in die Vorlage.
        function andereBereiche(zone) {
            return daten.value.zonen.filter(z => z.id !== zone.id);
        }

        function raumVerschieben(zone, ridx, zielId) {
            if (!zielId || zielId === zone.id) return;
            const ziel = daten.value.zonen.find(z => z.id === zielId);
            if (!ziel) return;
            const raum = zone.raeume[ridx];

            const kennung = raum.id || "";
            if (kennung && ziel.raeume.some(r => r.id === kennung)) {
                toast("Im Bereich \"" + (ziel.name || ziel.id) + "\" gibt es "
                      + "die Raum-Kennung \"" + kennung + "\" schon — bitte erst "
                      + "umbenennen.", "error");
                return;
            }

            const kreise = raum.heizkreise || [];
            const uebernimmt = kreise.filter(k =>
                k.basissollwert_c === null || k.basissollwert_c === undefined
                || k.verschiebung_max_k === null || k.verschiebung_max_k === undefined);
            let text = "Raum \"" + (raum.name || raum.id) + "\" nach \""
                + (ziel.name || ziel.id) + "\" verschieben?\n\n"
                + "Er folgt dann dem Zeitplan und der Absenkung des neuen "
                + "Bereichs. Fühler, Kontakte und Heizkreise ziehen mit.";
            if (uebernimmt.length) {
                text += "\n\nDie Aktor-Einstellungen sind im Regler je Kanal "
                    + "eingestellt und ändern sich durch den Umzug nicht. "
                    + uebernimmt.length + " Heizkreis(e) stehen auf \"wie "
                    + "Bereich\" — dort werden " + zone.basissollwert_c
                    + " °C / ±" + zone.verschiebung_max_k + " K fest eingetragen, "
                    + "damit sie nicht stillschweigend die Werte des neuen "
                    + "Bereichs übernehmen.";
            }
            if (!confirm(text)) return;

            kreise.forEach(k => {
                if (k.basissollwert_c === null || k.basissollwert_c === undefined) {
                    k.basissollwert_c = zone.basissollwert_c;
                }
                if (k.verschiebung_max_k === null || k.verschiebung_max_k === undefined) {
                    k.verschiebung_max_k = zone.verschiebung_max_k;
                }
            });

            zone.raeume.splice(ridx, 1);
            ziel.raeume.push(raum);
            // Die Aufklapp-Zustaende haengen am Index — nach dem Umzug stimmen
            // sie nicht mehr.
            raumOffen.value = {};
            toast("Verschoben. Noch nicht gespeichert — \"Speichern\" nicht "
                  + "vergessen.", "success");
        }

        function raumLage(zone, raum) {
            const l = lageNach.value[zone.id];
            if (!l || !l.raeume) return null;
            return l.raeume.find(r => r.raum === raum.id) || null;
        }

        // Was am Aktor unter "Eff. Sollwert" stehen MUSS, wenn Basis und
        // Verschiebungsbereich mit dem Geraet uebereinstimmen. Weicht der
        // angezeigte Wert ab, ist der Bereich im Aktor ein anderer als hier —
        // genau der Fall am 23.09.2026: hier ±5 K, im Aktor ±8 K, dadurch kam
        // aus +2,5 K am Geraet +4,0 K.
        function erwartetesW(zone, t) {
            const k = kreisLage(zone, t);
            if (!k || k.gesendet_verschiebung_k === null
                || k.gesendet_verschiebung_k === undefined) return "—";
            return (k.basissollwert_c + k.gesendet_verschiebung_k).toFixed(1);
        }

        function statusFarbe(s) { return (HEIZ_STATUS[s] || {}).farbe || "#94a3b8"; }
        function statusText(s) { return (HEIZ_STATUS[s] || {}).text || s; }

        // Kurz halten — die Kachel ist schmal. "42 s" / "3 min" / "nie".
        function gesendetText(zone, raum) {
            const l = raumLage(zone, raum);
            if (!l || !l.kreise || !l.kreise.length) return "nie";
            const zeiten = l.kreise
                .map(k => k.gesendet_vor_s)
                .filter(v => v !== null && v !== undefined);
            if (!zeiten.length) return "nie";
            const s = Math.round(Math.min(...zeiten));
            if (s < 90) return s + " s";
            if (s < 5400) return Math.round(s / 60) + " min";
            return Math.round(s / 3600) + " h";
        }

        // Aktortest: voller Sollwert, damit der Kreis aufmacht und man im
        // Verteiler sieht, ob der Stellmotor läuft. Wirkt sofort und ohne
        // Speichern — und endet von selbst, damit ein vergessener Test den
        // Raum nicht auf Anschlag weiterheizt.
        function testLaeuft(zone, raum) {
            const l = raumLage(zone, raum);
            return !!(l && l.test_aktiv);
        }
        function testRest(zone, raum) {
            const l = raumLage(zone, raum);
            if (!l || !l.test_rest_s) return "";
            return Math.max(1, Math.round(l.test_rest_s / 60)) + " min";
        }
        async function testUmschalten(zone, raum) {
            const an = !testLaeuft(zone, raum);
            try {
                const antwort = await api.post(
                    "/api/heizung/test?zone=" + encodeURIComponent(zone.id)
                    + "&raum=" + encodeURIComponent(raum.id)
                    + "&an=" + (an ? "true" : "false"));
                daten.value.lage = antwort.lage || [];
                if (antwort.warnung) {
                    toast(antwort.warnung, "error");
                } else {
                    toast(an ? ("Test läuft: " + (raum.name || raum.id)
                                + " bekommt den vollen Sollwert — endet von "
                                + "selbst nach 15 Minuten.")
                             : "Test beendet, der Raum regelt wieder nach Plan.",
                          "success");
                }
            } catch (e) {
                toast("Test nicht umgestellt: " + e.message
                      + " — Raum schon gespeichert?", "error");
            }
        }

        async function sendenErzwingen(zone, raum) {
            try {
                const pfad = "/api/heizung/senden?zone=" + encodeURIComponent(zone.id)
                    + (raum ? "&raum=" + encodeURIComponent(raum.id) : "");
                const antwort = await api.post(pfad);
                daten.value.lage = antwort.lage || [];
                const n = (antwort.gesendet || []).length;
                toast(n ? ("Gesendet an: " + antwort.gesendet.join(", "))
                        : "Nichts gesendet — siehe Hinweis am Raum",
                      n ? "success" : "error");
            } catch (e) {
                toast("Senden fehlgeschlagen: " + e.message, "error");
            }
        }
        function zielSetzen(t, wert) {
            const teile = (wert || "|").split("|");
            t.device_id = teile[0];
            t.channel_id = teile[1];
        }
        // Zu einem Heizkreis den Eintrag aus der Ausgangsliste finden. Die
        // Liste enthält seit 24.09.2026 JEDEN Heizungsaktor — auch solche, die
        // noch nicht senden können. Was einem fehlt, steht in `grund`.
        function zielInfo(t) {
            if (!t.device_id) return null;
            return ziele.value.find(z => z.device_id === t.device_id
                                      && z.channel_id === t.channel_id) || null;
        }
        function zielText(z) {
            const name = z.device + " · " + z.channel;
            if (z.bereit) return name;
            return name + (z.einrichtbar ? " — noch einzurichten"
                                         : " — sendet nicht");
        }
        async function zielEinrichten(t) {
            const z = zielInfo(t);
            if (!z) return;
            if (!confirm("Ausgang \"" + z.device + " · " + z.channel + "\" als "
                + "Heizausgang einrichten?\n\nDie im Aktor angelernte Adresse "
                + "wird dabei zur Sende-ID der Brücke und der Kanal auf Senden "
                + "gestellt. Das ändert die Geräteliste sofort — unabhängig "
                + "vom Speichern hier.")) return;
            try {
                const antwort = await api.post(
                    "/api/heizung/ziele/einrichten?device_id="
                    + encodeURIComponent(t.device_id)
                    + "&channel_id=" + encodeURIComponent(t.channel_id));
                ziele.value = antwort.ziele || [];
                toast("Eingerichtet: sendet als " + antwort.sender_id
                      + " über " + antwort.via_gateway, "success");
            } catch (e) {
                toast("Nicht eingerichtet: " + e.message, "error");
            }
        }
        function kreisLage(zone, t) {
            const l = lageNach.value[zone.id];
            if (!l || !l.raeume) return null;
            for (const r of l.raeume) {
                const k = (r.kreise || []).find(k =>
                    k.device_id === t.device_id && k.channel_id === t.channel_id);
                if (k) return k;
            }
            return null;
        }

        // Empfangene Werte nach "topic|feld" — die Liste kommt so vom Server,
        // weil ein Topic mehrere Felder tragen kann.
        const werteNach = computed(() => {
            const m = {};
            for (const e of (daten.value.istwerte || [])) {
                m[e.topic + "|" + (e.feld || "")] = e;
            }
            return m;
        });
        function wert(topic, feld) {
            if (!topic) return null;
            return werteNach.value[topic + "|" + (feld || "")] || null;
        }

        const lageNach = computed(() => {
            const m = {};
            for (const l of (daten.value.lage || [])) m[l.zone] = l;
            return m;
        });

        function statusInfo(zoneId) {
            const l = lageNach.value[zoneId];
            if (!l) return { text: "—", farbe: "#94a3b8" };
            return HEIZ_STATUS[l.status] || { text: l.status, farbe: "#94a3b8" };
        }

        // Was der Aktor mit dieser Basis und diesem Bereich ueberhaupt kann.
        // Geht die Uhr des Reglers gegen die des Browsers? Mehr als 5 Minuten
        // Unterschied heisst: der Zeitplan laeuft versetzt — genau der Fall vom
        // 24.09.2026, wo der Container auf UTC stand und die Tagsteuerung zwei
        // Stunden zu spaet kam.
        const uhrVersatz = computed(() => {
            if (!daten.value.regleruhr) return 0;
            const teile = daten.value.regleruhr.split(":");
            if (teile.length !== 2) return 0;
            const jetzt = new Date();
            const regler = Number(teile[0]) * 60 + Number(teile[1]);
            const hier = jetzt.getHours() * 60 + jetzt.getMinutes();
            let d = regler - hier;
            if (d > 720) d -= 1440;
            if (d < -720) d += 1440;
            return d;
        });

        function spanne(zone) {
            const b = Number(zone.basissollwert_c) || 0;
            const v = Number(zone.verschiebung_max_k) || 0;
            return (b - v).toFixed(1) + "–" + (b + v).toFixed(1) + " °C";
        }

        onMounted(() => {
            laden(); zieleLaden();
            timer = setInterval(() => laden(true), 10000);
        });
        onUnmounted(() => clearInterval(timer));

        return {
            daten, ziele, offen, laedt, WOCHENTAGE,
            laden, speichern, jetztSenden, neueZone, zoneLoeschen,
            neuesFenster, fensterLoeschen, tagUmschalten, wert,
            neuerFuehler, fuehlerLoeschen, neuerHeizkreis, zielSetzen, kreisLage,
            zielInfo, zielText, zielEinrichten, raumLoeschen,
            testLaeuft, testRest, testUmschalten,
            offsetSetzen, neuerKontakt, kontaktOffen, neuerRaum, raumLage,
            raumOffen, raumIstOffen, raumUmschalten, alleRaeume,
            restzeit, neuerSchalter, schalterLoeschen, schalterUmlegen,
            uhrVersatz, raumVerschieben, andereBereiche,
            technikOffen, statusFarbe, statusText, gesendetText,
            sendenErzwingen, erwartetesW,
            lageNach, statusInfo, spanne,
        };
    },
    template: [
        '<div class="page-header">',
        '  <div>',
        '    <h1>🌡 Heizung</h1>',
        '    <div class="subtitle">Bereiche mit Zeitzonen. Der Ist-Wert kommt aus MQTT (egal ob 1-Wire oder EnOcean), der Sollwert geht als Raumbedienteil-Telegramm an den Heizregler.</div>',
        '  </div>',
        '  <div style="display:flex; gap:0.5rem; align-items:center; white-space:nowrap">',
        '    <button class="btn btn-ghost" @click="jetztSenden">Jetzt senden</button>',
        '    <button class="btn btn-primary" @click="speichern" :disabled="laedt">Speichern</button>',
        '  </div>',
        '</div>',

        // Kopfzeile: Hauptschalter
        '<div class="card" style="display:flex; align-items:center; gap:1.5rem; flex-wrap:wrap">',
        '  <label style="display:flex; align-items:center; gap:0.5rem">',
        '    <input type="checkbox" v-model="daten.aktiv">',
        '    <b>Heizungsregelung aktiv</b>',
        '  </label>',
        '  <div style="font-size:0.8rem; color:var(--muted)">Takt <input class="input" type="number" v-model.number="daten.takt_s" min="5" max="300" style="width:5rem; display:inline-block"> s — wie oft nachgesehen wird, nicht wie oft gesendet wird.</div>',
        '  <div style="font-size:0.8rem; color:var(--muted)" title="Der Timberwolf führt jede Topic-Wurzel als eigenes MQTT-Gerät.">Wurzel-Topic <input class="input mono" v-model="daten.basis_topic" style="width:8rem; display:inline-block"></div>',
        '  <div style="font-size:0.8rem; color:var(--muted)" title="Zeitzone, in der die Uhrzeiten der Zeitzonen gelten. Sie steht hier und nicht am Container — sonst geht sie beim nächsten Neuaufbau verloren.">Zeitzone <input class="input mono" v-model="daten.zeitzone" style="width:9rem; display:inline-block"></div>',
        '  <div v-if="daten.regleruhr" :style="{ fontSize:\'0.8rem\', color: Math.abs(uhrVersatz) > 5 ? \'#dc2626\' : \'var(--muted)\' }" :title="Math.abs(uhrVersatz) > 5 ? \'Die Uhr des Reglers weicht von deiner ab — der Zeitplan läuft versetzt!\' : \'Uhr, nach der der Zeitplan läuft\'">',
        '    Regleruhr <b>{{ daten.regleruhr }}</b><span v-if="Math.abs(uhrVersatz) > 5"> ⚠ {{ uhrVersatz > 0 ? "+" : "" }}{{ uhrVersatz }} min gegenüber dir</span>',
        '  </div>',
        '  <span style="flex:1"></span>',
        '  <button class="btn btn-ghost btn-sm" @click="neueZone">+ Bereich</button>',
        '</div>',

        // Betriebsart-Schalter (z. B. "Gäste")
        '<div class="card" style="margin-top:1rem">',
        '  <div style="display:flex; align-items:center; gap:0.8rem; flex-wrap:wrap">',
        '    <h4 style="margin:0">Schalter</h4>',
        '    <span style="flex:1"></span>',
        '    <button class="btn btn-ghost btn-sm" @click="neuerSchalter">+ Schalter</button>',
        '  </div>',
        '  <div class="hinweistext" title="Der Stand steht in heizzonen.yaml und liegt retained auf <Wurzel>/schalter/<Kennung>/state — die Visu kann ihn anzeigen und über /set auch umlegen.">Betriebsarten, die über alle Bereiche hinweg gelten. Zeitzonen können daran hängen („nur wenn Gäste"), Räume auch („dauerhaft abgesenkt, außer wenn"). Umlegen wirkt sofort, ohne Speichern.</div>',
        '  <div v-if="!daten.schalter.length" class="hinweistext"><i>Noch kein Schalter angelegt.</i></div>',
        '  <div v-for="(s, sidx) in daten.schalter" :key="sidx" class="zeile" style="align-items:center">',
        '    <button class="btn btn-sm" :class="s.an ? \'btn-primary\' : \'btn-ghost\'" @click="schalterUmlegen(s)" style="width:5rem" :title="s.an ? \'ist an — klicken schaltet aus\' : \'ist aus — klicken schaltet ein\'">{{ s.an ? "● an" : "○ aus" }}</button>',
        '    <input class="input" v-model="s.name" placeholder="Gäste" style="width:10rem">',
        '    <input class="input mono" v-model="s.id" placeholder="gaeste" style="width:8rem" title="Kennung für MQTT und für die Verweise aus Zeitzonen und Räumen">',
        '    <span class="lageklein mono">{{ daten.basis_topic }}/schalter/{{ s.id }}/set</span>',
        '    <span style="flex:1"></span>',
        '    <button class="btn btn-ghost btn-sm" @click="schalterLoeschen(sidx)">🗑</button>',
        '  </div>',
        '</div>',

        '<div v-if="!daten.zonen.length" class="card" style="margin-top:1rem; color:var(--muted); font-style:italic">Noch kein Bereich angelegt.</div>',

        // Zonen
        '<div v-for="(zone, idx) in daten.zonen" :key="idx" class="card" style="margin-top:1rem">',
        '  <div style="display:flex; align-items:center; gap:0.8rem; cursor:pointer" @click="offen = (offen === idx ? null : idx)">',
        '    <span style="font-size:1.1rem">{{ offen === idx ? "▾" : "▸" }}</span>',
        '    <b style="flex:0 0 12rem">{{ zone.name || zone.id }}</b>',
        '    <span class="badge" :style="{ background: statusInfo(zone.id).farbe, color:\'#fff\', padding:\'0.1rem 0.5rem\', borderRadius:\'0.6rem\', fontSize:\'0.72rem\' }">{{ statusInfo(zone.id).text }}</span>',
        '    <span v-if="lageNach[zone.id]" style="font-size:0.85rem; color:var(--muted)">',
        '      Ist <b v-if="lageNach[zone.id].ist_c !== null">{{ lageNach[zone.id].ist_c }} °C</b><b v-else>—</b>',
        '      · Soll <b v-if="lageNach[zone.id].sollwert_c !== null">{{ lageNach[zone.id].sollwert_c }} °C</b><b v-else>—</b>',
        '      <span v-if="lageNach[zone.id].fenster"> ({{ lageNach[zone.id].fenster }})</span>',
        '      <span v-else-if="lageNach[zone.id].sollwert_quelle === \'letzter_wert\'"> (letzter Wert)</span>',
        '      <span v-if="lageNach[zone.id].verschiebung_k !== null"> · Verschiebung {{ lageNach[zone.id].verschiebung_k > 0 ? "+" : "" }}{{ lageNach[zone.id].verschiebung_k }} K</span>',
        '    </span>',
        '    <span style="flex:1"></span>',
        '    <button class="btn btn-ghost btn-sm" @click.stop="zoneLoeschen(idx)" title="Zone entfernen">🗑</button>',
        '  </div>',

        '  <div v-if="lageNach[zone.id] && lageNach[zone.id].hinweis" style="margin-top:0.5rem; font-size:0.8rem; padding:0.4rem 0.6rem; border-radius:var(--radius-sm); background:rgba(245,158,11,0.12)">{{ lageNach[zone.id].hinweis }}</div>',

        '  <div v-if="offen === idx" style="margin-top:1rem; border-top:1px solid var(--border); padding-top:1rem">',

        // Stammdaten
        '    <div style="display:flex; gap:1rem; flex-wrap:wrap">',
        '      <div style="flex:0 0 12rem"><label style="font-size:0.78rem; color:var(--muted)">Name</label><input class="input" v-model="zone.name"></div>',
        '      <div style="flex:0 0 10rem"><label style="font-size:0.78rem; color:var(--muted)">Kennung (MQTT)</label><input class="input mono" v-model="zone.id"></div>',
        '      <div style="flex:0 0 8rem; display:flex; align-items:end; padding-bottom:0.4rem"><label style="display:flex; gap:0.4rem; align-items:center"><input type="checkbox" v-model="zone.aktiv"> aktiv</label></div>',
        '    </div>',
        // --- Zeitplan ---
        '    <section class="abschnitt">',
        // Zeitplan
        '    <h4>Zeitzonen</h4>',
        '    <div class="hinweistext" title="Die Visu schickt alle Sollwertsteller einer Gruppe als ein JSON auf ein Topic, z. B. heizung/bereich1 mit dem Feld TMPTagWohnen. Überlappen sich Zeitzonen, gewinnt die zuletzt gestartete.">Nur Zeiten <b>mit</b> Heizbedarf anlegen — alles andere fällt auf den Rückfall-Sollwert darunter. Sollwert fest hinterlegt oder aus einem Sollwertsteller der Visu (Topic + Feld). Keine Tage angehakt = täglich.</div>',
        '    <table style="width:100%">',
        '      <thead><tr><th style="text-align:left">Name</th><th>von</th><th>bis</th><th style="text-align:left">Tage</th><th>Sollwert</th><th style="text-align:left">Sollwertsteller (Topic / Feld)</th><th style="text-align:left">nur wenn</th><th></th></tr></thead>',
        '      <tbody>',
        '        <tr v-for="(f, fidx) in zone.fenster" :key="fidx">',
        '          <td><input class="input" v-model="f.name" style="width:8rem"></td>',
        '          <td><input class="input mono" v-model="f.von" placeholder="06:00" style="width:5.5rem"></td>',
        '          <td><input class="input mono" v-model="f.bis" placeholder="09:00" style="width:5.5rem"></td>',
        '          <td>',
        '            <label v-for="t in WOCHENTAGE" :key="t.id" :style="{ display:\'inline-flex\', gap:\'0.2rem\', alignItems:\'center\', marginRight:\'0.4rem\', fontSize:\'0.75rem\', opacity: f.tage.length ? 1 : 0.5 }">',
        '              <input type="checkbox" :checked="f.tage.includes(t.id)" @change="tagUmschalten(f, t.id)">{{ t.label }}',
        '            </label>',
        '          </td>',
        '          <td style="white-space:nowrap"><input class="input" type="number" step="0.5" min="5" max="35" v-model.number="f.sollwert_c" style="width:5rem"> °C</td>',
        '          <td>',
        '            <div style="display:flex; gap:0.3rem">',
        '              <input class="input mono" v-model="f.sollwert_topic" placeholder="heizung/zone1" style="width:10rem">',
        '              <input class="input mono" v-model="f.sollwert_feld" placeholder="TMPTagWohnen" style="width:9rem">',
        '            </div>',
        '            <div v-if="wert(f.sollwert_topic, f.sollwert_feld)" style="font-size:0.7rem; color:var(--muted)">steht auf <b>{{ wert(f.sollwert_topic, f.sollwert_feld).wert }} °C</b></div>',
        '            <div v-else-if="f.sollwert_topic" style="font-size:0.7rem; color:#f59e0b">noch nichts empfangen — es gilt {{ f.sollwert_c }} °C</div>',
        '          </td>',
        '          <td>',
        '            <select class="input" :value="f.nur_wenn_schalter || \'\'" @change="f.nur_wenn_schalter = $event.target.value" style="width:8rem" title="Die Zeitzone gilt nur, solange dieser Schalter an ist. Zeigt sie auf einen gelöschten Schalter, gilt sie NICHT.">',
        '              <option value="">immer</option>',
        '              <option v-for="s in daten.schalter" :key="s.id" :value="s.id">{{ s.name || s.id }}</option>',
        '            </select>',
        '          </td>',
        '          <td><button class="btn btn-ghost btn-sm" @click="fensterLoeschen(zone, fidx)">🗑</button></td>',
        '        </tr>',
        '      </tbody>',
        '    </table>',
        '    <button class="btn btn-ghost btn-sm" style="margin-top:0.5rem" @click="neuesFenster(zone)">+ Zeitzone</button>',

        // Rueckfall-Sollwert fuer alle uebrigen Zeiten
        '    <div class="untertitel">Alle übrigen Zeiten <span>— was gilt, wenn keine Zeitzone läuft</span></div>',
        '    <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap">',
        '      <label style="display:flex; gap:0.3rem; align-items:center; font-size:0.78rem"><input type="checkbox" v-model="restzeit(zone).aktiv"> aktiv</label>',
        '      <input class="input" v-model="restzeit(zone).name" placeholder="Nachtabsenkung" style="width:9rem">',
        '      <span style="white-space:nowrap"><input class="input" type="number" step="0.5" min="5" max="35" v-model.number="restzeit(zone).sollwert_c" style="width:5rem"> °C</span>',
        '      <input class="input mono" v-model="restzeit(zone).sollwert_topic" placeholder="heizung/buerobereich1" style="width:11rem">',
        '      <input class="input mono" v-model="restzeit(zone).sollwert_feld" placeholder="BüroNacht" style="width:9rem">',
        '      <span v-if="wert(restzeit(zone).sollwert_topic, restzeit(zone).sollwert_feld)" class="lageklein">steht auf <b>{{ wert(restzeit(zone).sollwert_topic, restzeit(zone).sollwert_feld).wert }} °C</b></span>',
        '      <span v-else-if="restzeit(zone).sollwert_topic" class="lageklein" style="color:#f59e0b">noch nichts empfangen — es gilt {{ restzeit(zone).sollwert_c }} °C</span>',
        '    </div>',
        '    <div class="hinweistext">Ist das aus, bleibt außerhalb der Zeitzonen der <b>zuletzt gültige</b> Sollwert stehen. Dieser Wert ist zugleich das Ziel der Zwangsabsenkung — bei offenem Kontakt und bei dauerhaft abgesenkten Räumen.</div>',
        '    </section>',
        // --- Raeume ---
        '    <section class="abschnitt">',
        '    <h4>Räume</h4>',
        '    <div class="hinweistext" title="Jeder Raum hat seine Fühler, höchstens ein Sollwertrad, eigene Fensterkontakte und beliebig viele Heizkreise.">Klick auf ein Kästchen öffnet es. Anzeige: Ist → Soll · Heizkreise · zuletzt gesendet.</div>',
        '    <div v-if="!zone.raeume.length" class="hinweistext"><i>Noch kein Raum angelegt.</i></div>',

        '    <div class="raeume">',
        '      <div v-for="(raum, ridx) in zone.raeume" :key="ridx"',
        '           :class="[\'raum\', { offen: raumIstOffen(zone, ridx) }]">',

        // Kopfzeile — immer sichtbar
        '        <div class="kopf" @click="raumUmschalten(zone, ridx)">',
        '          <span>{{ raumIstOffen(zone, ridx) ? "▾" : "▸" }}</span>',
        '          <b style="flex:1">{{ raum.name || raum.id || "Raum" }}</b>',
        '          <span v-if="raumLage(zone, raum)" :style="{ background: statusFarbe(raumLage(zone, raum).status), color:\'#fff\', padding:\'0.02rem 0.35rem\', borderRadius:\'0.6rem\', fontSize:\'0.64rem\' }">{{ statusText(raumLage(zone, raum).status) }}</span>',
        '          <button class="btn btn-ghost btn-sm" style="padding:0 0.35rem" @click.stop="raumLoeschen(zone, ridx)" title="Raum entfernen">🗑</button>',
        '        </div>',
        '        <div class="werte" @click="raumUmschalten(zone, ridx)" style="cursor:pointer">',
        '          <span v-if="raumLage(zone, raum) && raumLage(zone, raum).ist_c !== null"><b>{{ raumLage(zone, raum).ist_c }}</b></span><span v-else>—</span>',
        '          <span v-if="raumLage(zone, raum) && raumLage(zone, raum).sollwert_wirksam_c !== null" :title="\'Zeitplan \' + raumLage(zone, raum).sollwert_c + \' °C + feste Korrektur/Verschiebung — das ist der Wert, der am Aktor ankommt\'"> → <b>{{ raumLage(zone, raum).sollwert_wirksam_c }}</b> °C</span>',
        '          <span v-else-if="raumLage(zone, raum) && raumLage(zone, raum).sollwert_c !== null"> → <b>{{ raumLage(zone, raum).sollwert_c }}</b> °C</span>',
        '          <span v-if="raumLage(zone, raum) && raumLage(zone, raum).raumsteller_k" :title="raumLage(zone, raum).raumsteller_quelle === \'hand\' ? \'Verschiebung von Hand\' : \'Sollwertrad\'"> {{ raumLage(zone, raum).raumsteller_k > 0 ? "+" : "" }}{{ raumLage(zone, raum).raumsteller_k }}K<span style="color:var(--muted)">{{ raumLage(zone, raum).raumsteller_quelle === "hand" ? " (Hand)" : "" }}</span></span>',
        '          <b v-if="testLaeuft(zone, raum)" style="color:#dc2626" title="Aktortest läuft — voller Sollwert"> 🔧 Test</b>',
        '          <b v-if="raumLage(zone, raum) && raumLage(zone, raum).dauerabsenkung" style="color:#dc2626" title="Dauerhaft abgesenkt"> ⏻↓</b>',
        '          <b v-else-if="raumLage(zone, raum) && raumLage(zone, raum).abgesenkt" style="color:#dc2626" title="Wegen offenem Kontakt abgesenkt"> ↓</b>',
        '          <span :title="raum.heizkreise.length + \' Heizkreis(e), zuletzt gesendet\'"> · {{ raum.heizkreise.length }} HK · {{ gesendetText(zone, raum) }}</span>',
        '        </div>',
        '        <div v-if="raumLage(zone, raum) && raumLage(zone, raum).hinweis" class="hinweistext" style="margin:0.3rem 0 0">{{ raumLage(zone, raum).hinweis }}</div>',

        // Inhalt — im selben Kaestchen
        '        <div v-show="raumIstOffen(zone, ridx)">',

        '          <div class="untertitel" style="border-top:none; padding-top:0.4rem">Raum</div>',
        '          <div style="display:flex; gap:0.4rem; align-items:center; flex-wrap:wrap">',
        '            <input class="input" v-model="raum.name" placeholder="Wohnzimmer" style="width:9rem">',
        '            <input class="input mono" v-model="raum.id" placeholder="wohnzimmer" style="width:8rem">',
        '            <label style="display:flex; gap:0.3rem; align-items:center; font-size:0.74rem" title="Aus = der Raum funkt NICHT mehr. Der Aktor meldet nach 90 Minuten Sensorausfall und bleibt auf der letzten Stellgröße stehen. Zum Stilllegen besser „dauerhaft abgesenkt“."><input type="checkbox" v-model="raum.aktiv"> aktiv</label>',
        '            <span style="flex:1"></span>',
        '            <select v-if="daten.zonen.length > 1" class="input" style="width:11rem" :value="\'\'" @change="raumVerschieben(zone, ridx, $event.target.value); $event.target.value = \'\'" title="Raum mitsamt Fühlern, Kontakten und Heizkreisen in einen anderen Bereich verschieben">',
        '              <option value="">verschieben nach …</option>',
        '              <option v-for="z in andereBereiche(zone)" :key="z.id" :value="z.id">{{ z.name || z.id }}</option>',
        '            </select>',
        '            <button class="btn btn-ghost btn-sm" @click.stop="sendenErzwingen(zone, raum)" title="Sendetakt zurücksetzen und sofort funken">📡 senden</button>',
        '            <button class="btn btn-ghost btn-sm" @click.stop="raumLoeschen(zone, ridx)" title="Raum entfernen">🗑 Raum entfernen</button>',
        '          </div>',
        // Aktortest — voller Sollwert auf Zeit, zum Zusehen im Verteiler
        '          <div style="display:flex; gap:0.4rem; align-items:center; margin-top:0.3rem; flex-wrap:wrap">',
        '            <label style="display:flex; gap:0.3rem; align-items:center; font-size:0.74rem; cursor:pointer" title="Schickt dem Aktor den größtmöglichen Sollwert, damit der Kreis aufmacht — zum Nachsehen im Verteiler, ob der Stellmotor läuft. Wirkt sofort, ohne Speichern, und endet nach 15 Minuten von selbst.">',
        '              <input type="checkbox" :checked="testLaeuft(zone, raum)" @change.stop="testUmschalten(zone, raum)"> Aktor testen',
        '            </label>',
        '            <span v-if="testLaeuft(zone, raum)" class="lageklein" style="color:#dc2626"><b>Test läuft</b> — noch {{ testRest(zone, raum) }}, dann regelt der Raum wieder nach Plan.</span>',
        '            <span v-else class="lageklein">Kreis öffnet auf vollen Sollwert, 15 Minuten lang.</span>',
        '          </div>',

        '          <div class="untertitel">Fühler <span>— mehrere werden verrechnet</span></div>',
        '          <div v-for="(f, sidx) in raum.fuehler" :key="sidx" class="zeile">',
        '            <input class="input" v-model="f.name" placeholder="Name" style="width:5.5rem">',
        '            <input class="input mono" v-model="f.topic" placeholder="1wire/sensors/…" style="flex:1 1 11rem">',
        '            <input class="input mono" v-model="f.feld" placeholder="Feld" style="width:5.5rem">',
        '            <input class="input" type="number" step="0.5" min="0.5" v-model.number="f.gewicht" style="width:3.2rem" title="Gewicht im Mittelwert">',
        '            <button class="btn btn-ghost btn-sm" @click="raum.fuehler.splice(sidx,1)">🗑</button>',
        '            <div class="lageklein"><span v-if="wert(f.topic, f.feld)"><b>{{ wert(f.topic, f.feld).wert }}</b> vor {{ Math.round(wert(f.topic, f.feld).alter_s) }} s</span><span v-else style="color:#b45309">nichts empfangen</span></div>',
        '          </div>',
        '          <div style="display:flex; gap:0.4rem; align-items:center; margin-top:0.2rem; flex-wrap:wrap">',
        '            <button class="btn btn-ghost btn-sm" @click="neuerFuehler(raum)">+ Fühler</button>',
        '            <select class="input" :value="raum.fuehler_regel || \'\'" @change="raum.fuehler_regel = $event.target.value || null" style="width:11rem" title="Wie mehrere Fühler zu einem Wert werden"><option value="">wie Bereich</option><option value="mittelwert">Mittelwert</option><option value="kleinster">kältester</option><option value="groesster">wärmster</option></select>',
        '            <span style="font-size:0.72rem; color:var(--muted)" title="Dauerhafte Eigenschaft des Raums: Fühler hängt zu kalt, oder der Raum soll grundsätzlich wärmer sein (Bad +2 K). Bleibt auch dann stehen, wenn später ein Sollwertrad dazukommt.">feste Korrektur</span>',
        '            <input class="input" type="number" step="0.1" v-model.number="raum.sollwert_offset_k" style="width:4.2rem" title="Fühlerlage oder Nutzung — z. B. Bad +2 K"> <span style="font-size:0.72rem; color:var(--muted)">K</span>',
        '          </div>',

        // Sollwertverschiebung von Hand + Dauerabsenkung
        '          <div style="display:flex; gap:0.4rem; align-items:center; margin-top:0.4rem; flex-wrap:wrap">',
        '            <span style="font-size:0.72rem; color:var(--muted)" title="Das Sollwertrad in Software — für Räume ohne Rad an der Wand. Gleiche Grenze wie das Rad; sobald ein echtes Rad Werte liefert, gilt dessen Stellung statt dieser.">Verschiebung von Hand</span>',
        '            <input class="input" type="number" step="0.5" :min="-zone.offset_grenze_k" :max="zone.offset_grenze_k" v-model.number="raum.handverschiebung_k" style="width:4.2rem"> <span style="font-size:0.72rem; color:var(--muted)">K (±{{ zone.offset_grenze_k }})</span>',
        '            <span v-if="raumLage(zone, raum) && raumLage(zone, raum).raumsteller_quelle === \'rad\' && raum.handverschiebung_k" class="lageklein" style="color:#f59e0b">unwirksam — das Sollwertrad gilt</span>',
        '            <span style="flex:1"></span>',
        '            <label style="display:flex; gap:0.3rem; align-items:center; font-size:0.74rem" title="Der Raum fährt auf den Rückfall-Sollwert des Bereichs und funkt dabei weiter — für längere Nichtnutzung (z. B. Gästezimmer)."><input type="checkbox" v-model="raum.dauerabsenkung"> dauerhaft abgesenkt</label>',
        '            <select v-if="raum.dauerabsenkung" class="input" :value="raum.dauerabsenkung_ausnahme_schalter || \'\'" @change="raum.dauerabsenkung_ausnahme_schalter = $event.target.value" style="width:11rem" title="Solange dieser Schalter an ist, gilt die Absenkung nicht.">',
        '              <option value="">immer abgesenkt</option>',
        '              <option v-for="s in daten.schalter" :key="s.id" :value="s.id">außer wenn {{ s.name || s.id }}</option>',
        '            </select>',
        '          </div>',

        '          <div class="untertitel">Sollwertrad <span>— höchstens eins, z. B. der Thanos</span></div>',
        '          <div class="zeile">',
        '            <input class="input" :value="raum.offset_quelle ? raum.offset_quelle.name : \'\'" @input="offsetSetzen(raum, \'name\', $event.target.value)" placeholder="Name" style="width:5.5rem">',
        '            <input class="input mono" :value="raum.offset_quelle ? raum.offset_quelle.topic : \'\'" @input="offsetSetzen(raum, \'topic\', $event.target.value)" placeholder="enocean/…/state" style="flex:1 1 11rem">',
        '            <input class="input mono" :value="raum.offset_quelle ? raum.offset_quelle.feld : \'\'" @input="offsetSetzen(raum, \'feld\', $event.target.value)" placeholder="setpoint_offset_c" style="width:8rem">',
        '            <div v-if="raum.offset_quelle && wert(raum.offset_quelle.topic, raum.offset_quelle.feld)" class="lageklein">steht auf <b>{{ wert(raum.offset_quelle.topic, raum.offset_quelle.feld).wert > 0 ? "+" : "" }}{{ wert(raum.offset_quelle.topic, raum.offset_quelle.feld).wert }} K</b></div>',
        '          </div>',

        '          <div class="untertitel">Fensterkontakte <span>— senken nur diesen Raum ab</span></div>',
        '          <div v-for="(k, kidx) in raum.kontakte" :key="kidx" class="zeile">',
        '            <input class="input" v-model="k.name" placeholder="Name" style="width:5.5rem">',
        '            <input class="input mono" v-model="k.topic" placeholder="haus/…/fenster" style="flex:1 1 9rem">',
        '            <input class="input mono" v-model="k.feld" placeholder="closed" style="width:5rem">',
        '            <select class="input" v-model="k.bedeutung" style="width:6.5rem" title="Beschreibt das Feld den offenen oder den geschlossenen Zustand?"><option value="geschlossen">geschl.</option><option value="offen">offen</option></select>',
        '            <span v-if="kontaktOffen(k)" style="color:#dc2626; font-size:0.72rem"><b>offen</b></span>',
        '            <button class="btn btn-ghost btn-sm" @click="raum.kontakte.splice(kidx,1)">🗑</button>',
        '          </div>',
        '          <button class="btn btn-ghost btn-sm" @click="neuerKontakt(raum)">+ Fensterkontakt</button>',

        '          <div class="untertitel">Heizkreise <span>— je ein Ausgang des Reglers</span></div>',
        '          <div v-if="!ziele.length" class="hinweistext" style="color:#b45309">Kein Heizungsaktor gefunden — in der Geräteliste muss einer angelegt sein.</div>',
        '          <div v-for="(t, tidx) in raum.heizkreise" :key="tidx" class="zeile" style="flex-wrap:wrap">',
        '            <input class="input" v-model="t.name" placeholder="Name" style="width:5.5rem">',
        '            <select class="input" style="flex:1 1 10rem" :value="t.device_id + \'|\' + t.channel_id" @change="zielSetzen(t, $event.target.value)" title="Alle Heizungsaktoren, die die Brücke kennt. Steht dahinter „noch einzurichten“, fehlt dem Kanal nur der Sendeweg.">',
        '              <option value="|">— Ausgang wählen —</option>',
        '              <option v-for="z in ziele" :key="z.device_id + z.channel_id" :value="z.device_id + \'|\' + z.channel_id">{{ zielText(z) }}</option>',
        '            </select>',
        '            <input class="input" type="number" step="0.1" v-model.number="t.basissollwert_c" placeholder="Basis" style="width:4rem" title="Basissollwert dieses Ausgangs — leer = wie Bereich">',
        '            <input class="input" type="number" step="1" min="1" max="20" v-model.number="t.verschiebung_max_k" placeholder="±K" style="width:3.2rem" title="Verschiebungsbereich dieses Ausgangs — leer = wie Bereich">',
        '            <button class="btn btn-ghost btn-sm" @click="raum.heizkreise.splice(tidx,1)" title="Heizkreis entfernen">🗑</button>',
        '            <div class="lageklein" v-if="kreisLage(zone, t)">',
        '              <span v-if="kreisLage(zone, t).sendbar">{{ kreisLage(zone, t).verschiebung_k > 0 ? "+" : "" }}{{ kreisLage(zone, t).verschiebung_k }} K · vor {{ Math.round(kreisLage(zone, t).gesendet_vor_s) }} s · <span :title="\'Am Gerät unter „Eff. Sollwert“ gegenprüfen — weicht W ab, stimmt der Verschiebungsbereich nicht mit dem Aktor überein.\'">W: <b>{{ erwartetesW(zone, t) }}</b></span></span>',
        '              <span v-else style="color:#dc2626">{{ kreisLage(zone, t).hinweis || "regelt nicht" }}</span>',
        '            </div>',
        // Was dem gewählten Ausgang fehlt — und der Knopf, der es behebt.
        '            <div v-if="zielInfo(t) && !zielInfo(t).bereit" style="flex:1 1 100%; display:flex; gap:0.4rem; align-items:center; flex-wrap:wrap">',
        '              <span class="lageklein" style="color:#b45309">{{ zielInfo(t).grund }}</span>',
        '              <button v-if="zielInfo(t).einrichtbar" class="btn btn-sm btn-primary" @click="zielEinrichten(t)" title="Adresse des Aktors als Sende-ID übernehmen und den Kanal auf Senden stellen">Ausgang einrichten</button>',
        '            </div>',
        '            <div v-else-if="zielInfo(t) && zielInfo(t).spanne_k" class="lageklein" style="flex:1 1 100%" title="So steht es im Produktkatalog. Maßgeblich ist, was im Aktor eingestellt ist — dort gegenprüfen.">Katalog: Basis {{ zielInfo(t).basis_c }} °C · ±{{ zielInfo(t).spanne_k }} K</div>',
        '          </div>',
        '          <button class="btn btn-ghost btn-sm" @click="neuerHeizkreis(raum)">+ Heizkreis</button>',
        '        </div>',
        '      </div>',
        '    </div>',
        '    <div style="display:flex; gap:0.5rem; margin-top:0.4rem">',
        '      <button class="btn btn-ghost btn-sm" @click="neuerRaum(zone)">+ Raum</button>',
        '      <button class="btn btn-ghost btn-sm" v-if="zone.raeume.length > 1" @click="alleRaeume(zone, true)">alle öffnen</button>',
        '      <button class="btn btn-ghost btn-sm" v-if="zone.raeume.length > 1" @click="alleRaeume(zone, false)">alle schließen</button>',
        '    </div>',
        '    </section>',
        // --- Absenken bei offener Tuer ---
        '    <section class="abschnitt">',
        // Türkontakte des Bereichs
        '    <h4>Absenken bei offener Tür</h4>',
        '    <div class="hinweistext" title="„Bedeutung“ sagt, was das Feld beschreibt: ein EnOcean-Fensterkontakt meldet closed, also „geschlossen“. Meldet das Feld direkt Text wie open/closed, gilt der Text und die Einstellung ist gegenstandslos.">Solange einer dieser Kontakte offen ist, gilt für alle Räume der Absenk-Sollwert — unabhängig vom Zeitplan.</div>',
        '    <table v-if="zone.kontakte.length" style="width:100%">',
        '      <thead><tr><th style="text-align:left">Name</th><th style="text-align:left">MQTT-Topic</th><th style="text-align:left">Feld</th><th style="text-align:left">Bedeutung</th><th style="text-align:left">Zustand</th><th></th></tr></thead>',
        '      <tbody>',
        '        <tr v-for="(k, kidx) in zone.kontakte" :key="kidx">',
        '          <td><input class="input" v-model="k.name" placeholder="Schiebetür" style="width:9rem"></td>',
        '          <td><input class="input mono" v-model="k.topic" placeholder="enocean/raum_a/…/state" style="width:20rem"></td>',
        '          <td><input class="input mono" v-model="k.feld" placeholder="closed" style="width:8rem"></td>',
        '          <td><select class="input" v-model="k.bedeutung" style="width:9rem"><option value="geschlossen">geschlossen</option><option value="offen">offen</option></select></td>',
        '          <td style="font-size:0.78rem; color:var(--muted)"><span v-if="kontaktOffen(k)" style="color:#dc2626"><b>offen</b></span><span v-else-if="wert(k.topic, k.feld)">zu</span><span v-else style="color:#f59e0b">nichts empfangen</span></td>',
        '          <td><button class="btn btn-ghost btn-sm" @click="zone.kontakte.splice(kidx,1)">🗑</button></td>',
        '        </tr>',
        '      </tbody>',
        '    </table>',
        '    <div style="display:flex; gap:1rem; align-items:end; margin-top:0.4rem; flex-wrap:wrap">',
        '      <button class="btn btn-ghost btn-sm" @click="neuerKontakt(zone)">+ Kontakt</button>',
        '      <div><label style="font-size:0.78rem; color:var(--muted)">Absenken auf Zeitzone</label><select class="input" v-model="zone.absenkung_zeitzone" style="width:14rem"><option value="">{{ restzeit(zone).aktiv ? "— " + (restzeit(zone).name || "alle übrigen Zeiten") + " —" : "— feste Absenkung —" }}</option><option v-for="f in zone.fenster" :key="f.name" :value="f.name">{{ f.name }}</option></select></div>',
        '      <div v-if="!zone.absenkung_zeitzone && !restzeit(zone).aktiv" style="flex:0 0 8rem"><label style="font-size:0.78rem; color:var(--muted)">Absenkung K</label><input class="input" type="number" step="0.5" min="0" v-model.number="zone.absenkung_k"></div>',
        '    </div>',
        '    </section>',
        // --- Technik (zuklappbar) ---
        '    <section class="abschnitt">',
        '      <div style="cursor:pointer; display:flex; align-items:center; gap:0.5rem" @click="technikOffen = (technikOffen === zone.id ? null : zone.id)">',
        '        <span>{{ technikOffen === zone.id ? "▾" : "▸" }}</span>',
        '        <b style="font-size:0.9rem">Abgleich mit dem Heizregler</b>',
        '        <span style="font-size:0.78rem; color:var(--muted)">Basis {{ zone.basissollwert_c }} °C · ±{{ zone.verschiebung_max_k }} K · Takt {{ zone.sende_intervall_s }} s · erreichbar {{ spanne(zone) }}</span>',
        '      </div>',
        '      <div v-show="technikOffen === zone.id" style="margin-top:0.7rem">',
        // Aktor-Parameter
        '    <h4>Abgleich mit dem Heizregler</h4>',
        '    <div class="hinweistext" title="Das Telegramm überträgt nur einen Anteil des im Aktor eingestellten Bereichs, keinen Sollwert. Stimmt der Bereich nicht überein, regelt der Aktor auf etwas anderes, ohne dass ein Fehler auftaucht.">Basis und Bereich müssen <b>genau so</b> im Aktor stehen — sonst regelt er auf einen anderen Wert. Gegenprobe am Gerät: „Eff. Sollwert“ muss dem W je Heizkreis entsprechen.</div>',
        '    <div style="display:flex; gap:1rem; flex-wrap:wrap; align-items:end">',
        '      <div style="flex:0 0 9rem"><label style="font-size:0.78rem; color:var(--muted)">Basissollwert (°C)</label><input class="input" type="number" step="0.1" v-model.number="zone.basissollwert_c"></div>',
        '      <div style="flex:0 0 9rem"><label style="font-size:0.78rem; color:var(--muted)">Verschiebung ± (K)</label><input class="input" type="number" step="1" min="1" max="20" v-model.number="zone.verschiebung_max_k"></div>',
        '      <div style="flex:0 0 9rem"><label style="font-size:0.78rem; color:var(--muted)">Korrektur (K)</label><input class="input" type="number" step="0.1" v-model.number="zone.sollwert_offset_k"></div>',
        '      <div style="flex:0 0 9rem"><label style="font-size:0.78rem; color:var(--muted)">Sendetakt (s)</label><input class="input" type="number" step="30" min="30" max="3600" v-model.number="zone.sende_intervall_s"></div>',
        '      <div style="font-size:0.8rem; color:var(--muted); padding-bottom:0.5rem">erreichbar: <b>{{ spanne(zone) }}</b></div>',
        '    </div>',
        '    <div style="display:flex; gap:1rem; flex-wrap:wrap; align-items:end; margin-top:0.5rem">',
        '      <div style="flex:0 0 9rem"><label style="font-size:0.78rem; color:var(--muted)">max. Alter Ist (s)</label><input class="input" type="number" v-model.number="zone.ist_max_alter_s" min="0"></div>',
        '      <div style="flex:0 0 12rem"><label style="font-size:0.78rem; color:var(--muted)" title="Werte außerhalb gelten als Störung der Quelle und werden wie ein fehlender Wert behandelt.">plausibel von … bis (°C)</label><div style="display:flex; gap:0.3rem"><input class="input" type="number" step="1" v-model.number="zone.ist_min_c"><input class="input" type="number" step="1" v-model.number="zone.ist_max_c"></div></div>',
        '      <div style="flex:0 0 9rem"><label style="font-size:0.78rem; color:var(--muted)">Sollwertrad ± K</label><input class="input" type="number" step="0.5" min="0" max="10" v-model.number="zone.offset_grenze_k"></div>',
        '      <div><label style="font-size:0.78rem; color:var(--muted)">Fühler verrechnen (Vorgabe)</label><select class="input" v-model="zone.fuehler_regel" style="width:11rem"><option value="mittelwert">Mittelwert</option><option value="kleinster">kältester Fühler</option><option value="groesster">wärmster Fühler</option></select></div>',
        '    </div>',
        '    <div v-if="zone.sende_intervall_s > 3600" style="font-size:0.78rem; color:#dc2626">Achtung: Der Regler meldet nach 90 Minuten ohne Telegramm Sensorausfall.</div>',
        '      </div>',
        '    </section>',
        '  </div>',
        '</div>',
    ].join("\n"),
};

const TabSettings = {
    setup() {
        const mqtt = reactive({
            host: "", port: 1883, username: "", password: "",
            base_topic: "enocean", qos: 1, retain_state: true,
        });
        const ptmOnPress = ref("I");
        const energyMaxJump = ref(1000);
        const powerMaxW = ref(50000);
        const flowMaxM3h = ref(100);
        const flowMaxLMin = ref(1000);
        const telegramAudit = ref(true);
        const loading = ref(true);
        const saving = ref(false);

        async function load() {
            loading.value = true;
            try {
                const c = await api.get("/api/config");
                Object.assign(mqtt, c.mqtt || {});
                const d = c.defaults || {};
                ptmOnPress.value = d.ptm_on_press || "I";
                energyMaxJump.value = d.energy_max_jump_kwh != null ? d.energy_max_jump_kwh : 1000;
                powerMaxW.value = d.power_max_w != null ? d.power_max_w : 50000;
                flowMaxM3h.value = d.flow_max_m3h != null ? d.flow_max_m3h : 100;
                flowMaxLMin.value = d.flow_max_l_min != null ? d.flow_max_l_min : 1000;
                telegramAudit.value = d.telegram_audit_enabled !== false;
            } catch (e) {
                toast("Einstellungen laden fehlgeschlagen: " + e.message, "error");
            } finally {
                loading.value = false;
            }
        }
        async function save() {
            saving.value = true;
            try {
                const r = await api.put("/api/config", {
                    mqtt: {
                        host: (mqtt.host || "").trim(),
                        port: Number(mqtt.port) || 1883,
                        username: mqtt.username || "",
                        password: mqtt.password || "",
                        base_topic: (mqtt.base_topic || "enocean").trim(),
                        qos: Number(mqtt.qos),
                        retain_state: !!mqtt.retain_state,
                    },
                    defaults: {
                        ptm_on_press: ptmOnPress.value,
                        energy_max_jump_kwh: Number(energyMaxJump.value) || 0,
                        power_max_w: Number(powerMaxW.value) || 0,
                        flow_max_m3h: Number(flowMaxM3h.value) || 0,
                        flow_max_l_min: Number(flowMaxLMin.value) || 0,
                        telegram_audit_enabled: !!telegramAudit.value,
                    },
                });
                toast("Gespeichert" + (r.mqtt_reconnected ? " · MQTT neu verbunden" : ""), "success");
            } catch (e) {
                toast("Speichern fehlgeschlagen: " + e.message, "error");
            } finally {
                saving.value = false;
            }
        }
        onMounted(load);
        return { mqtt, ptmOnPress, energyMaxJump, powerMaxW, flowMaxM3h,
                 flowMaxLMin, telegramAudit, loading, saving, save, load };
    },
    template: [
        '<div>',
        '  <div class="page-head"><h1>⚙ Einstellungen</h1></div>',
        '  <div v-if="loading" style="color: var(--muted)">Lädt …</div>',
        '  <template v-else>',
        '    <div class="card" style="padding: 1rem; margin-bottom: 1rem; max-width: 640px">',
        '      <h3 style="margin: 0 0 0.7rem 0; font-size: 1rem">MQTT-Server</h3>',
        '      <div style="display: grid; grid-template-columns: 9rem 1fr; gap: 0.5rem 0.7rem; align-items: center">',
        '        <label style="font-size: 0.82rem">Host / IP</label>',
        '        <input class="input mono" v-model="mqtt.host" placeholder="192.168.1.59">',
        '        <label style="font-size: 0.82rem">Port</label>',
        '        <input class="input mono" type="number" v-model="mqtt.port" placeholder="1883" style="max-width: 8rem">',
        '        <label style="font-size: 0.82rem">Base-Topic</label>',
        '        <input class="input mono" v-model="mqtt.base_topic" placeholder="enocean">',
        '        <label style="font-size: 0.82rem">Benutzer</label>',
        '        <input class="input" v-model="mqtt.username" placeholder="(leer = anonym)">',
        '        <label style="font-size: 0.82rem">Passwort</label>',
        '        <input class="input" type="password" v-model="mqtt.password" placeholder="(leer = keins)">',
        '        <label style="font-size: 0.82rem">QoS</label>',
        '        <select class="input" v-model="mqtt.qos" style="max-width: 8rem">',
        '          <option :value="0">0</option><option :value="1">1</option><option :value="2">2</option>',
        '        </select>',
        '        <label style="font-size: 0.82rem">Retain</label>',
        '        <label style="font-size: 0.82rem; display: flex; align-items: center; gap: 0.4rem"><input type="checkbox" v-model="mqtt.retain_state"> State-Topics retained veröffentlichen</label>',
        '      </div>',
        '      <div style="font-size: 0.74rem; color: var(--muted); margin-top: 0.6rem">Änderungen werden sofort übernommen — die MQTT-Verbindung verbindet sich live neu (kein Container-Neustart nötig).</div>',
        '    </div>',
        '    <div class="card" style="padding: 1rem; margin-bottom: 1rem; max-width: 640px">',
        '      <h3 style="margin: 0 0 0.5rem 0; font-size: 1rem">PTM-Schalter-Polung (global)</h3>',
        '      <div style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.6rem">Welche Wippen-Hälfte schaltet EIN? Gilt als Standard für alle Funktaster. Einzelne Schalter lassen sich im Geräte-Edit-Dialog abweichend einstellen.</div>',
        '      <label style="display: flex; align-items: center; gap: 0.4rem; margin-bottom: 0.3rem; cursor: pointer"><input type="radio" value="I" v-model="ptmOnPress"> <b>oben</b> schaltet EIN — Eltako-Standard</label>',
        '      <label style="display: flex; align-items: center; gap: 0.4rem; cursor: pointer"><input type="radio" value="0" v-model="ptmOnPress"> <b>unten</b> schaltet EIN</label>',
        '    </div>',
        '    <div class="card" style="padding: 1rem; margin-bottom: 1rem; max-width: 640px">',
        '      <h3 style="margin: 0 0 0.5rem 0; font-size: 1rem">Energiezähler-Ausreißerschutz (global)</h3>',
        '      <div style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.6rem">Größter plausibler Sprung eines Zählerstands zwischen zwei Telegrammen. Größere Sprünge nach oben gelten als Funk-/Bus-Störung und werden verworfen — so kann ein einzelner Fehlwert den Zählerstand nicht dauerhaft „nach oben festhängen". 0 = Schutz aus.</div>',
        '      <label style="display: flex; align-items: center; gap: 0.4rem"><input type="number" class="input" style="max-width: 9rem" min="0" step="100" v-model="energyMaxJump"> <span style="color: var(--muted)">kWh</span></label>',
        '    </div>',
        '    <div class="card" style="padding: 1rem; margin-bottom: 1rem; max-width: 640px">',
        '      <h3 style="margin: 0 0 0.5rem 0; font-size: 1rem">Momentanwert-/Durchfluss-Ausreißerschutz (global)</h3>',
        '      <div style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.7rem">Obergrenzen für Momentan-/Durchflusswerte (Leistung, Gas, Wasser). Ein einzelnes gestörtes Funktelegramm (z.B. Gas-Durchfluss 524288) würde sonst im Anzeigefeld „einfrieren", weil solche Werte selten kommen und nie überschrieben werden. Werte darüber werden verworfen — das auslösende Roh-Telegramm bleibt im Telegramm-Protokoll sichtbar. 0 = Schutz aus.</div>',
        '      <div style="display: grid; grid-template-columns: 12rem 1fr; gap: 0.5rem 0.7rem; align-items: center">',
        '        <label style="font-size: 0.82rem">Max. Leistung</label>',
        '        <label style="display: flex; align-items: center; gap: 0.4rem"><input type="number" class="input" style="max-width: 9rem" min="0" step="1000" v-model="powerMaxW"> <span style="color: var(--muted)">W</span></label>',
        '        <label style="font-size: 0.82rem">Max. Gas-Durchfluss</label>',
        '        <label style="display: flex; align-items: center; gap: 0.4rem"><input type="number" class="input" style="max-width: 9rem" min="0" step="10" v-model="flowMaxM3h"> <span style="color: var(--muted)">m³/h</span></label>',
        '        <label style="font-size: 0.82rem">Max. Wasser-Durchfluss</label>',
        '        <label style="display: flex; align-items: center; gap: 0.4rem"><input type="number" class="input" style="max-width: 9rem" min="0" step="50" v-model="flowMaxLMin"> <span style="color: var(--muted)">L/min</span></label>',
        '      </div>',
        '    </div>',
        '    <div class="card" style="padding: 1rem; margin-bottom: 1rem; max-width: 640px">',
        '      <h3 style="margin: 0 0 0.5rem 0; font-size: 1rem">Telegramm-Voll-Protokoll</h3>',
        '      <div style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.6rem">Schreibt ALLE empfangenen Telegramme mit Roh-Payload dauerhaft nach <code>telegram_audit.log</code> (täglich rotierend, ~6 Wochen). Damit lässt sich ein später auffallender Fehlwert bis zum auslösenden Telegramm zurückverfolgen. Änderung wirkt nach dem nächsten Neustart.</div>',
        '      <label style="display: flex; align-items: center; gap: 0.4rem; cursor: pointer"><input type="checkbox" v-model="telegramAudit"> Voll-Protokoll aktiv</label>',
        '    </div>',
        '    <button class="btn btn-primary" :disabled="saving" @click="save">{{ saving ? "Speichert …" : "Speichern" }}</button>',
        '  </template>',
        '</div>',
    ].join("\n"),
};

// ===== App =====
const AppRoot = {
    setup() {
        const currentTab = ref("dashboard");
        const tabs = [
            { id: "dashboard",  icon: "📊", label: "Dashboard" },
            { id: "devices",    icon: "🔌", label: "Geräte" },
            { id: "heizung",    icon: "🌡", label: "Heizung" },
            { id: "gateways",   icon: "📡", label: "Gateways" },
            { id: "live",       icon: "📜", label: "Live-Log" },
            { id: "teach",      icon: "🎓", label: "Anlernen" },
            { id: "calculator", icon: "🔢", label: "ID-Rechner" },
            { id: "settings",   icon: "⚙",  label: "Einstellungen" },
        ];

        const version = ref("");
        onMounted(async () => {
            try {
                const info = await api.get("/api/info");
                version.value = info.version;
                state.info = info;
            } catch (e) { /* silent */ }
        });

        return {
            tabs,
            currentTab,
            version,
            toast: computed(() => state.toast),
        };
    },
    components: {
        "tab-dashboard":  TabDashboard,
        "tab-devices":    TabDevices,
        "tab-heizung":    TabHeizung,
        "tab-gateways":   TabGateways,
        "tab-live":       TabLive,
        "tab-teach":      TabTeach,
        "tab-calculator": TabCalculator,
        "tab-settings":   TabSettings,
    },
    template: [
        '<div class="app">',
        '  <aside class="sidebar">',
        '    <div class="brand">',
        '      <img src="/assets/logo-mark.svg" class="brand-mark" alt="Triumvirat">',
        '      <div>',
        '        <div class="brand-name">enocean2mqtt</div>',
        '        <div class="brand-sub">v{{ version || "..." }}</div>',
        '      </div>',
        '    </div>',
        '    <div',
        '      v-for="tab in tabs" :key="tab.id"',
        '      :class="[\'nav-item\', { active: currentTab === tab.id }]"',
        '      @click="currentTab = tab.id"',
        '    >',
        '      <span class="icon">{{ tab.icon }}</span>',
        '      <span>{{ tab.label }}</span>',
        '    </div>',
        '    <div style="flex: 1"></div>',
        '    <div style="font-size: 0.72rem; color: var(--muted); padding: 0 0.5rem;">',
        '      Community-Tool · bereitgestellt von Triumvirat IT',
        '    </div>',
        '  </aside>',
        '  <main class="content">',
        '    <component :is="\'tab-\' + currentTab"></component>',
        '  </main>',
        '  <transition name="toast">',
        '    <div v-if="toast" :class="[\'toast\', toast.type]">{{ toast.text }}</div>',
        '  </transition>',
        '</div>',
    ].join("\n"),
};

// EEP-Profile beim Start laden, dann App mounten
loadProfiles().finally(() => {
    createApp(AppRoot).mount("#app");
});
