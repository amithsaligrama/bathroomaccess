function render(m) {
    var ret = "<h3>" + m.name + "</h3><br><b>Address</b>: <a target=\"_blank\" href=\"https://www.google.com/maps/search/?api=1&query=" + m.latitude + "%2C" + m.longitude + "\">" + m.address + "</a><br><b>Hours</b>: " + m.hours + "<br><b>Remarks</b>: " + m.remarks.replace(/(?:\r\n|\r|\n)/g, '<br>');;
    console.log(ret);
    return ret;
}

const attribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
const map = L.map('map')
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: attribution }).addTo(map);
const markers = JSON.parse(document.getElementById('markers-data').textContent);
let markerGroup = L.featureGroup([]).addTo(map);

for (var key in markers) {
    var latlng = L.latLng(markers[key].fields.latitude, markers[key].fields.longitude);
    L.marker(latlng).bindPopup(render(markers[key].fields)).addTo(markerGroup);
}

map.fitBounds(markerGroup.getBounds(), { maxZoom: 15, padding: [100, 100] });
