from flask import *
import requests
import time
import json
from math import *
import rhino3dm as rh
from pyproj import *
import mapbox_vector_tile
import mercantile
import base64
import concurrent.futures

application = Flask(__name__, static_url_path='/static', static_folder='static')
application.secret_key = 'nettletontribe_secret_key'

mapbox_access_token = 'pk.eyJ1Ijoicml2aW5kdWIiLCJhIjoiY2xmYThkcXNjMHRkdDQzcGU4Mmh2a3Q3MSJ9.dXlhamKyYyGusL3PWqDD9Q'

compute_url = "http://13.54.229.195:80/"
headers = {
    "RhinoComputeKey": "8c96f7d9-5a62-4bbf-ad3f-6e976b94ea1e"
}

class __Rhino3dmEncoder(json.JSONEncoder):
    def default(self, o):
        if hasattr(o, "Encode"):
            return o.Encode()
        return json.JSONEncoder.default(self, o)


def create_boundary(lat, lon, distance):
    R = 6371.0
    lat_r = radians(lat)
    ns_dist = distance / (R * 1000)
    ew_dist = distance / (R * 1000 * cos(lat_r))
    max_lat = lat + ns_dist
    min_lat = lat - ns_dist
    max_lon = lon + ew_dist
    min_lon = lon - ew_dist
    return min_lon, max_lon, min_lat, max_lat


def create_layer(model, name, color):
    layer = rh.Layer()
    layer.Name = name
    layer.Color = color
    return model.Layers.Add(layer)


def encode_ghx_file(file_path):
    with open(file_path, mode="r", encoding="utf-8-sig") as file:
        gh_contents = file.read()
        gh_bytes = gh_contents.encode("utf-8")
        gh_encoded = base64.b64encode(gh_bytes)
        gh_decoded = gh_encoded.decode("utf-8")
    return gh_decoded


def create_parameters(geometry, geometry_type, xmin_LL, ymin_LL, xmax_LL, ymax_LL):
    params = {
        'where': '1=1',
        'geometry': f'{geometry}',
        'geometryType': f'{geometry_type}',
        'spatialRel': 'esriSpatialRelIntersects',
        'returnGeometry': 'true',
        'f': 'json',
        'outFields': '*',
        'inSR': '4326',
        'outSR': '32756'
    }
    if geometry_type == 'esriGeometryEnvelope':
        params['geometry'] = f'{xmin_LL}, {ymin_LL}, {xmax_LL}, {ymax_LL}'
    return params


def get_data(url, params):
    counter = 0
    while True:
        response = requests.get(url, params)
        if response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    data = json.loads(response.text)
    return data


def add_to_model(data, layerIndex, p_key, r_key, model):
    if 'features' not in data:
        return

    for feature in data["features"]:
        try:
            value = feature['attributes'][p_key]
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                att = rh.ObjectAttributes()
                att.LayerIndex = layerIndex
                att.SetUserString(r_key, str(value))
                model.Objects.AddCurve(curve, att)
        except KeyError:
            return jsonify({'error': True})


def fetch_mapbox_data(zoom, tile):
    mb_url = f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{zoom}/{tile.x}/{tile.y}.mvt?access_token={mapbox_access_token}"
    counter = 0
    while True:
        mb_response = requests.get(mb_url)
        if mb_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    mb_data = mb_response.content
    return mb_data


def concurrent_fetching(zoom, tile):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(fetch_mapbox_data, zoom, tile)
        result = future.result()
    return result


def send_compute_post(payload):
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=payload, headers=headers)
        if res.status_code == 200:
            return res
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)


def process_feature(feature, p_key, curves, numbers):
    number = feature['attributes'][p_key]
    if number is not None:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            curves.append(curve)
            numbers.append(number)


def add_bound_curve_to_model(data, model, layerIndex):
    curve = None
    counter = 0
    while True:
        if 'features' in data:
            for feature in data["features"]:
                geometry = feature["geometry"]
                for ring in geometry["rings"]:
                    points = []
                    for coord in ring:
                        point = rh.Point3d(coord[0], coord[1], 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    att = rh.ObjectAttributes()
                    att.LayerIndex = layerIndex
                    model.Objects.AddCurve(curve, att)
                    return curve
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)


def add_mesh_to_model(data, layerIndex, p_key, paramName, gh_algo, gh_param, model):
    curves = []
    numbers = []

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = []

        for feature in data["features"]:
            futures.append(executor.submit(process_feature,
                                           feature, p_key, curves, numbers))

        for future in concurrent.futures.as_completed(futures):
            future.result()

    curves_data = [{"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_data[0]["InnerTree"][key] = value

    names_data = [{"ParamName": paramName, "InnerTree": {}}]
    for i, number in enumerate(numbers):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": number
            }
        ]
        names_data[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_algo,
        "pointer": None,
        "values": curves_data + names_data
    }

    sorted_names = []
    res = send_compute_post(geo_payload)
    if hasattr(res, 'content'):
        response_object = json.loads(res.content)['values']
        for val in response_object:
            if val['ParamName'] == gh_param:
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            sorted_names.append(data)

        i = 0
        for val in response_object:
            if val['ParamName'] == 'RH_OUT:Mesh':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = layerIndex
                            att.SetUserString(paramName, sorted_names[i])
                            model.Objects.AddMesh(geo, att)
                            i += 1


transformer2 = Transformer.from_crs("EPSG:4326", "EPSG:32756", always_xy=True)
transformer = Transformer.from_crs("EPSG:3857", "EPSG:32756")

application.config['UPLOAD_FOLDER'] = '/upload'


@application.route('/', methods=['GET', 'POST'])
def index():
    return render_template('index.html')


@application.route('/submit/planning', methods=['POST'])
def get_planning():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 20000)
    z_xmin_LL, z_xmax_LL, z_ymin_LL, z_ymax_LL = create_boundary(
        lat, lon, 40000)
    b_xmin_LL, b_xmax_LL, b_ymin_LL, b_ymax_LL = create_boundary(
        lat, lon, 50000)
    p_xmin_LL, p_xmax_LL, p_ymin_LL, p_ymax_LL = create_boundary(
        lat, lon, 500000)
    a_xmin_LL, a_xmax_LL, a_ymin_LL, a_ymax_LL = create_boundary(
        lat, lon, 5000000)
    n_xmin_LL, n_xmax_LL, n_ymin_LL, n_ymax_LL = create_boundary(
        lat, lon, 800000)

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters('', 'esriGeometryEnvelope',
                               xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    b_params = create_parameters(
        '', 'esriGeometryEnvelope', b_xmin_LL, b_ymin_LL, b_xmax_LL, b_ymax_LL)
    a_params = create_parameters(
        '', 'esriGeometryEnvelope', a_xmin_LL, a_ymin_LL, a_xmax_LL, a_ymax_LL)
    p_params = create_parameters(
        '', 'esriGeometryEnvelope', p_xmin_LL, p_ymin_LL, p_xmax_LL, p_ymax_LL)
    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    def add_curves_to_model(data, transformer, layerIndex, model):
        curves = []
        for feature in data['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'Polygon':
                geometry = feature['geometry']['coordinates']
                for ring in geometry:
                    points = []
                    for coord in ring:
                        iso_x, iso_y = coord[0], coord[1]
                        iso_x, iso_y = transformer.transform(iso_x, iso_y)
                        point = rh.Point3d(iso_x, iso_y, 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    curves.append(curve)

        curves_data = [{"ParamName": "Curves", "InnerTree": {}}]
        for i, curve in enumerate(curves):
            serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "Rhino.Geometry.Curve",
                    "data": serialized_curve
                }
            ]
            curves_data[0]["InnerTree"][key] = value

        geo_payload = {
            "algo": gh_interpolate_decoded,
            "pointer": None,
            "values": curves_data
        }
        counter = 0
        while True:
            res = requests.post(compute_url + "grasshopper",
                                json=geo_payload, headers=headers)
            if res.status_code == 200:
                break
            else:
                counter += 1
                if counter >= 3:
                    return jsonify({'error': True})
                time.sleep(0)

        response_object = json.loads(res.content)['values']
        for val in response_object:
            paramName = val['ParamName']
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = layerIndex
                        model.Objects.AddCurve(geo, att)

    z_params = {
        'where': '1=1',
        'geometry': f'{z_xmin_LL}, {z_ymin_LL},{z_xmax_LL},{z_ymax_LL}',
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelContains',
        'returnGeometry': 'true',
        'f': 'json',
        'outFields': '*',
        'inSR': '4326',
        'outSR': '32756',

    }
    native_post = {
        'maps': 'territories',
        'polygon_geojson': {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'properties': {},
                    'geometry': {
                        'type': 'Polygon',
                        'coordinates': [
                            [
                                [n_xmin_LL, n_ymin_LL],
                                [n_xmax_LL, n_ymin_LL],
                                [n_xmax_LL, n_ymax_LL],
                                [n_xmin_LL, n_ymax_LL],
                                [n_xmin_LL, n_ymin_LL]
                            ]
                        ]
                    }
                }
            ]
        }
    }

    planning_model = rh.File3dm()
    planning_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(
        planning_model, "Boundary", (237, 0, 194, 255))
    admin_layerIndex = create_layer(
        planning_model, "Administrative Boundaries", (134, 69, 255, 255))
    native_layerIndex = create_layer(
        planning_model, "Native Land", (134, 69, 255, 255))
    zoning_layerIndex = create_layer(
        planning_model, "Zoning", (255, 180, 18, 255))
    hob_layerIndex = create_layer(
        planning_model, "HoB", (204, 194, 173, 255))
    lotsize_layerIndex = create_layer(
        planning_model, "Minimum Lot Size", (224, 155, 177, 255))
    fsr_layerIndex = create_layer(
        planning_model, "FSR", (173, 35, 204, 255))
    lots_layerIndex = create_layer(
        planning_model, "Lots", (255, 106, 0, 255))
    plan_extent_layerIndex = create_layer(
        planning_model, "Plan Extent", (178, 255, 0, 255))
    road_layerIndex = create_layer(
        planning_model, "Roads", (145, 145, 145, 255))
    walking_layerIndex = create_layer(
        planning_model, "Walking Isochrone", (129, 168, 0, 255))
    cycling_layerIndex = create_layer(
        planning_model, "Cycling Isochrone", (0, 168, 168, 255))
    driving_layerIndex = create_layer(
        planning_model, "Driving Isochrone", (168, 0, 121, 255))
    acid_layerIndex = create_layer(
        planning_model, "Acid Sulfate Soils", (133, 82, 227, 255))
    bushfire_layerIndex = create_layer(
        planning_model, "Bushfire", (176, 7, 7, 255))
    flood_layerIndex = create_layer(
        planning_model, "Flood", (113, 173, 201, 255))
    heritage_layerIndex = create_layer(
        planning_model, "Heritage", (153, 153, 153, 255))
    airport_layerIndex = create_layer(
        planning_model, "Airport", (255, 128, 227, 255))
    parks_layerIndex = create_layer(
        planning_model, "Parks", (0, 204, 0, 255))
    procedural_layerIndex = create_layer(
        planning_model, "Geometry", (0, 204, 0, 255))

    gh_fsr_decoded = encode_ghx_file(r"./gh_scripts/fsr.ghx")
    gh_hob_decoded = encode_ghx_file(r"./gh_scripts/hob.ghx")
    gh_mls_decoded = encode_ghx_file(r"./gh_scripts/mls.ghx")
    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/zoning.ghx")
    gh_interpolate_decoded = encode_ghx_file(
        r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_procedural_decoded = encode_ghx_file(r"./gh_scripts/procedural.ghx")

    adminboundaries_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Administrative_Boundaries/MapServer/0/query'
    zoning_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/2/query"
    hob_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/5/query"
    lotsize_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/4/query"
    fsr_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/1/query"
    lots_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    plan_extent_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/8/query'
    acid_url = "https://mapprod1.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Protection_Layers/MapServer/0/query"
    bushfire_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/ePlanning/Planning_Portal_Hazard/MapServer/229/query"
    flood_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/ePlanning/Planning_Portal_Hazard/MapServer/230/query"
    heritage_url = "https://mapprod1.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/0/query"
    airport_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/Protection/MapServer/6/query"
    parks_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/EDP/Estate/MapServer/0/query"
    native_url = 'https://native-land.ca/wp-json/nativeland/v1/api/index.php'

    params_dict = {
        adminboundaries_url: params,
        zoning_url: z_params,
        hob_url: z_params,
        lotsize_url: z_params,
        fsr_url: z_params,
        lots_url: params,
        plan_extent_url: params,
        acid_url: params,
        bushfire_url: b_params,
        flood_url: b_params,
        heritage_url: z_params,
        airport_url: a_params,
        parks_url: p_params

    }

    urls = [
        adminboundaries_url,
        zoning_url,
        hob_url,
        lotsize_url,
        fsr_url,
        lots_url,
        plan_extent_url,
        acid_url,
        bushfire_url,
        flood_url,
        heritage_url,
        airport_url,
        parks_url
    ]

    data_dict = {}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(
            get_data, url, params=params_dict[url]): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            data = future.result()
            if data is not None:
                if url == adminboundaries_url:
                    data_dict['admin_data'] = data
                elif url == zoning_url:
                    data_dict['zoning_data'] = data
                elif url == hob_url:
                    data_dict['hob_data'] = data
                elif url == lotsize_url:
                    data_dict['lotsize_data'] = data
                elif url == fsr_url:
                    data_dict['fsr_data'] = data
                elif url == lots_url:
                    data_dict['lots_data'] = data
                elif url == plan_extent_url:
                    data_dict['plan_extent_data'] = data
                elif url == acid_url:
                    data_dict['acid_data'] = data
                elif url == bushfire_url:
                    data_dict['bushfire_data'] = data
                elif url == flood_url:
                    data_dict['flood_data'] = data
                elif url == heritage_url:
                    data_dict['heritage_data'] = data
                elif url == airport_url:
                    data_dict['airport_data'] = data
                elif url == parks_url:
                    data_dict['parks_data'] = data

    admin_data = data_dict.get('admin_data')
    zoning_data = data_dict.get('zoning_data')
    hob_data = data_dict.get('hob_data')
    lotsize_data = data_dict.get('lotsize_data')
    fsr_data = data_dict.get('fsr_data')
    lots_data = data_dict.get('lots_data')
    plan_extent_data = data_dict.get('plan_extent_data')
    acid_data = data_dict.get('acid_data')
    bushfire_data = data_dict.get('bushfire_data')
    flood_data = data_dict.get('flood_data')
    heritage_data = data_dict.get('heritage_data')
    airport_data = data_dict.get('airport_data')
    parks_data = data_dict.get('parks_data')

    boundary_data = get_data(boundary_url, boundary_params)
    bound_curve = add_bound_curve_to_model(
        boundary_data, planning_model, boundary_layerIndex)

    counter = 0
    while True:
        native_response = requests.post(
            native_url, json=native_post)
        if native_response.status_code == 200:
            break
        else:
            time.sleep(0)
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    native_data = native_response.json()
    for feature in native_data:
        geometry = feature['geometry']
        properties = feature['properties']
        name = properties['Name']
        for ring in geometry['coordinates']:
            points = []
            for coord in ring:
                native_x, native_y = transformer2.transform(
                    coord[0], coord[1])
                point = rh.Point3d(native_x, native_y, 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = native_layerIndex
            att.SetUserString("Native Name", str(name))
            planning_model.Objects.AddCurve(curve, att)

    add_to_model(admin_data, admin_layerIndex,
                 'suburbname', 'Suburb', planning_model)

    def process_zoning_feature(feature, zoning_curves, zoning_names):
        zoning_name = feature['attributes']['SYM_CODE']
        geometry = feature["geometry"]
        points = []
        for coord in geometry["rings"][0]:
            point = rh.Point3d(coord[0], coord[1], 0)
            points.append(point)
        polyline = rh.Polyline(points)
        curve = polyline.ToNurbsCurve()
        zoning_curves.append(curve)
        zoning_names.append(zoning_name)

    def process_zoning_data(zoning_data, gh_zoning_decoded, layerIndex, model):
        zoning_curves = []
        zoning_names = []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            for feature in zoning_data["features"]:
                futures.append(executor.submit(
                    process_zoning_feature, feature, zoning_curves, zoning_names))
            for future in concurrent.futures.as_completed(futures):
                future.result()
        curves_zoning = [{"ParamName": "Curves", "InnerTree": {}}]
        for i, curve in enumerate(zoning_curves):
            serialized_curve = json.dumps(
                curve, cls=__Rhino3dmEncoder)
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "Rhino.Geometry.Curve",
                    "data": serialized_curve
                }
            ]
            curves_zoning[0]["InnerTree"][key] = value
        names_zoning = [{"ParamName": "Zoning", "InnerTree": {}}]
        for i, zone in enumerate(zoning_names):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": zone
                }
            ]
            names_zoning[0]["InnerTree"][key] = value
        geo_payload = {
            "algo": gh_zoning_decoded,
            "pointer": None,
            "values": curves_zoning + names_zoning
        }
        zoning_names_sorted = []
        res = send_compute_post(geo_payload)
        response_object = json.loads(res.content)['values']
        for val in response_object:
            paramName = val['ParamName']
            if paramName == 'RH_OUT:Zone':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            zoning_names_sorted.append(data)
        i = 0
        for val in response_object:
            paramName = val['ParamName']
            if paramName == 'RH_OUT:Mesh':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = layerIndex
                            att.SetUserString(
                                "Zoning Code", zoning_names_sorted[i])
                            model.Objects.AddMesh(
                                geo, att)
                            i += 1

    process_zoning_data(
        zoning_data, gh_zoning_decoded, zoning_layerIndex, planning_model)

    hob_nums = []
    hob_curves = []
    counter = 0
    while True:
        hob_response = requests.get(hob_url, params=z_params)
        if hob_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)

    hob_data = json.loads(hob_response.text)
    if "features" in hob_data:
        for feature in hob_data["features"]:
            hob_num = feature['attributes']['MAX_B_H']
            if hob_num is None:
                continue
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                hob_curves.append(curve)
                hob_nums.append(str(hob_num))
    else:
        time.sleep(0)

    curves_hob = [{"ParamName": "Curves", "InnerTree": {}}]
    for i, curve in enumerate(hob_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_hob[0]["InnerTree"][key] = value

    numbers_hob = [{"ParamName": "HOB", "InnerTree": {}}]
    for i, hob in enumerate(hob_nums):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": hob
            }
        ]
        numbers_hob[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_hob_decoded,
        "pointer": None,
        "values": curves_hob + numbers_hob
    }

    hob_numbers_sorted = []
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter > 1:
                break
            time.sleep(0)
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:HOBnum':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        hob_numbers_sorted.append(data)

    i = 0
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = hob_layerIndex
                        att.SetUserString("HOB", str(hob_numbers_sorted[i]))
                        planning_model.Objects.AddMesh(geo, att)
                        i += 1

    add_mesh_to_model(lotsize_data, lotsize_layerIndex, 'LOT_SIZE',
                      'MLS', gh_mls_decoded, 'RH_OUT:MLSnum', planning_model)

    add_mesh_to_model(fsr_data, fsr_layerIndex, 'FSR', 'FSR',
                      gh_fsr_decoded, 'RH_OUT:FSRnum', planning_model)

    add_to_model(lots_data, lots_layerIndex,
                 "plannumber", "Lot Number", planning_model)

    add_to_model(plan_extent_data, plan_extent_layerIndex,
                 "planoid", "Plan Extent Number", planning_model)

    add_to_model(acid_data, acid_layerIndex,
                 "LAY_CLASS", "Acid Class", planning_model)

    add_to_model(bushfire_data, bushfire_layerIndex,
                 "d_Category", "Bushfire Class", planning_model)

    add_to_model(flood_data, flood_layerIndex,
                 "LAY_CLASS", "Flood Class", planning_model)

    add_to_model(heritage_data, heritage_layerIndex,
                 "H_NAME", "Heritage Name", planning_model)

    for feature in airport_data["features"]:
        min_height = feature['attributes']['MINIMUM_HEIGHT']
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(
                    coord[0], coord[1], min_height)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = airport_layerIndex
            att.SetUserString(
                "Minimum Height", str(min_height))
            planning_model.Objects.AddCurve(
                curve, att)

    add_to_model(parks_data, parks_layerIndex,
                 "NAME", "Park", planning_model)

    road_curves = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)
        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                points = []
                for ring in geometry:
                    x_val, y_val = ring[0], ring[1]
                    x_prop = (x_val / 4096)
                    y_prop = (y_val / 4096)
                    lon_delta = lon2 - lon1
                    lat_delta = lat2 - lat1
                    lon_mapped = lon1 + (x_prop * lon_delta)
                    lat_mapped = lat1 + (y_prop * lat_delta)
                    lon_mapped, lat_mapped = transformer2.transform(
                        lon_mapped, lat_mapped)
                    point = rh.Point3d(lon_mapped, lat_mapped, 0)
                    points.append(point)

                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                road_curves.append(curve)

            elif geometry_type == 'MultiLineString':
                geometry = feature['geometry']['coordinates']
                for line_string in geometry:
                    points = []
                    for ring in line_string:
                        x_val, y_val = ring[0], ring[1]
                        x_prop = (x_val / 4096)
                        y_prop = (y_val / 4096)
                        lon_delta = lon2 - lon1
                        lat_delta = lat2 - lat1
                        lon_mapped = lon1 + (x_prop * lon_delta)
                        lat_mapped = lat1 + (y_prop * lat_delta)
                        lon_mapped, lat_mapped = transformer2.transform(
                            lon_mapped, lat_mapped)
                        point = rh.Point3d(
                            lon_mapped, lat_mapped, 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    road_curves.append(curve)

    curves_list_roads = [{"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(road_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_list_roads[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_roads_decoded,
        "pointer": None,
        "values": curves_list_roads
    }

    res = send_compute_post(geo_payload)
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Roads':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = road_layerIndex
                        planning_model.Objects.AddCurve(geo, att)

    # hob_data = get_data(hob_url, boundary_params)
    # if "features" in hob_data:
    #     for feature in hob_data["features"]:
    #         hob_num = feature['attributes']['MAX_B_H']
    #         if hob_num is None:
    #             hob_num = 3
    # else:
    #     time.sleep(0)

    # fsr_data = get_data(fsr_url, boundary_params)
    # if "features" in fsr_data:
    #     for feature in fsr_data["features"]:
    #         fsr_num = feature['attributes']['FSR']
    #         if fsr_num is None:
    #             fsr_num = 0.5
    # else:
    #     time.sleep(0)

    # hob_list = [{"ParamName": "HOB", "InnerTree": {}}]
    # hobs_list = []
    # hobs_list.append(hob_num)

    # fsr_list = [{"ParamName": "FSR", "InnerTree": {}}]
    # fsrs_list = []
    # fsrs_list.append(fsr_num)

    # bound_list = [{"ParamName": "Boundary", "InnerTree": {}}]
    # bounds_list = []
    # bounds_list.append(bound_curve)

    # for i, num in enumerate(hobs_list):
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "System.Float",
    #             "data": num
    #         }
    #     ]
    #     hob_list[0]["InnerTree"][key] = value

    # for i, num in enumerate(fsrs_list):
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "System.Float",
    #             "data": num
    #         }
    #     ]
    #     fsr_list[0]["InnerTree"][key] = value

    # for i, curve in enumerate(bounds_list):
    #     serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "Rhino.Geometry.Curve",
    #             "data": serialized_curve
    #         }
    #     ]
    #     bound_list[0]["InnerTree"][key] = value

    # geo_payload = {
    #     "algo": gh_procedural_decoded,
    #     "pointer": None,
    #     "values": bound_list + fsr_list + hob_list
    # }

    # res = send_compute_post(geo_payload)
    # response_object = json.loads(res.content)['values']
    # for val in response_object:
    #     paramName = val['ParamName']
    #     if paramName == 'RH_OUT:Geometry':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     geo = rh.CommonObject.Decode(data)
    #                     att = rh.ObjectAttributes()
    #                     att.LayerIndex = procedural_layerIndex
    #                     planning_model.Objects.AddBrep(geo, att)

    profile1 = 'mapbox/walking'
    profile2 = 'mapbox/cycling'
    profile3 = 'mapbox/driving'
    longitude_iso = lon
    latitude_iso = lat

    iso_url_w = f'https://api.mapbox.com/isochrone/v1/{profile1}/{longitude_iso},{latitude_iso}?contours_minutes=5&polygons=true&access_token={mapbox_access_token}'

    iso_url_c = f'https://api.mapbox.com/isochrone/v1/{profile2}/{longitude_iso},{latitude_iso}?contours_minutes=10&polygons=true&access_token={mapbox_access_token}'

    iso_url_d = f'https://api.mapbox.com/isochrone/v1/{profile3}/{longitude_iso},{latitude_iso}?contours_minutes=15&polygons=true&access_token={mapbox_access_token}'

    counter = 0
    while True:
        iso_response_w = requests.get(iso_url_w)
        if iso_response_w.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)

    walking_data = json.loads(iso_response_w.content.decode())

    counter = 0
    while True:
        iso_response_c = requests.get(iso_url_c)
        if iso_response_c.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    cycling_data = json.loads(iso_response_c.content.decode())

    counter = 0
    while True:
        iso_response_d = requests.get(iso_url_d)
        if iso_response_d.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    driving_data = json.loads(iso_response_d.content.decode())

    add_curves_to_model(walking_data, transformer2,
                        walking_layerIndex, planning_model)
    add_curves_to_model(cycling_data, transformer2,
                        cycling_layerIndex, planning_model)
    add_curves_to_model(driving_data, transformer2,
                        driving_layerIndex, planning_model)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in planning_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "planning.3dm"
    planning_model.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/submit/geometry', methods=['POST'])
def get_geometry():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 20000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)
    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)
    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    geometry_model = rh.File3dm()
    geometry_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(
        geometry_model, "Boundary", (237, 0, 194, 255))
    building_layerIndex = create_layer(
        geometry_model, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(
        geometry_model, "Contours", (191, 191, 191, 255))
    geometry_layerIndex = create_layer(
        geometry_model, "Geometry", (191, 191, 191, 255))

    params_dict = {
        boundary_url: boundary_params,
        topo_url: topo_params
    }

    urls = [
        boundary_url,
        topo_url
    ]

    data_dict = {}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(
            get_data, url, params=params_dict[url]): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            data = future.result()
            if data is not None:
                if url == topo_url:
                    data_dict['topography_data'] = data
                elif url == boundary_url:
                    data_dict['boundary_data'] = data

    topography_data = data_dict.get('topography_data')
    boundary_data = data_dict.get('boundary_data')

    bound_curve = add_bound_curve_to_model(
        boundary_data, geometry_model, boundary_layerIndex)

    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'building' in tiles1:
            building_layerMB = tiles1['building']

            tile1 = mercantile.Tile(tile.x, tile.y, 16)
            bbox = mercantile.bounds(tile1)
            lon1, lat1, lon2, lat2 = bbox

            for feature in building_layerMB['features']:
                geometry_type = feature['geometry']['type']
                height = feature['properties']['height']
                if geometry_type == 'Polygon':
                    geometry = feature['geometry']['coordinates']
                    for ring in geometry:
                        points = []
                        for coord in ring:
                            x_val, y_val = coord[0], coord[1]
                            x_prop = (x_val / 4096)
                            y_prop = (y_val / 4096)
                            lon_delta = lon2 - lon1
                            lat_delta = lat2 - lat1
                            lon_mapped = lon1 + \
                                (x_prop * lon_delta)
                            lat_mapped = lat1 + \
                                (y_prop * lat_delta)
                            lon_mapped, lat_mapped = transformer2.transform(
                                lon_mapped, lat_mapped)
                            point = rh.Point3d(
                                lon_mapped, lat_mapped, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        geometry_model.Objects.AddExtrusion(
                            extrusion, att)
                elif geometry_type == 'MultiPolygon':
                    geometry = feature['geometry']['coordinates']
                    for polygon in geometry:
                        for ring in polygon:
                            points = []
                            for coord in ring:
                                x_val, y_val = coord[0], coord[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                            polyline = rh.Polyline(points)
                            curve = polyline.ToNurbsCurve()
                            orientation = curve.ClosedCurveOrientation()
                            if str(orientation) == 'CurveOrientation.Clockwise':
                                curve.Reverse()
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            geometry_model.Objects.AddExtrusion(
                                extrusion, att)
        else:
            time.sleep(0)

    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['elevation']
            geometry = feature["geometry"]
            for ring in geometry["paths"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                att = rh.ObjectAttributes()
                att.LayerIndex = contours_layerIndex
                att.SetUserString(
                    "Elevation", str(elevation))
                geometry_model.Objects.AddCurve(curve, att)
    else:
        time.sleep(0)

    giraffe_file = request.files['uploadGiraffeBtn']
    if giraffe_file:
        sub_layers = {}
        giraffe_file_path = 'tmp/files/' + giraffe_file.filename
        giraffe_file.save(giraffe_file_path)
        with open(giraffe_file_path) as f:
            data = json.load(f)
        for feature in data['features']:
            geometry_type = feature.get('geometry', {}).get('type')
            layer_id = feature.get('properties', {}).get('layerId')
            usage = feature.get('properties', {}).get('usage')
            if geometry_type == 'Point':
                x, y = feature['geometry']['coordinates']
                x, y = transformer2.transform(x, y)
                circle_center = rh.Point3d(x, y, 0)
                circle_radius = 1.0
                circle = rh.Circle(circle_center, circle_radius)

                extrusion = rh.Extrusion.Create(
                    circle.ToNurbsCurve(), 10, True)

                top_point = rh.Point3d(x, y, 10)
                sphere = rh.Sphere(top_point, 2.5)

                if layer_id is None:
                    continue

                if layer_id not in sub_layers:
                    sub_layer_name = f"Giraffe:{layer_id}"
                    sub_layer = rh.Layer()
                    sub_layer.Name = sub_layer_name
                    sub_layers[layer_id] = geometry_model.Layers.Add(
                        sub_layer)

                att = rh.ObjectAttributes()
                att.LayerIndex = sub_layers[layer_id]
                att.SetUserString("Usage", str(usage))
                geometry_model.Objects.AddExtrusion(extrusion, att)
                geometry_model.Objects.AddSphere(sphere, att)

            elif geometry_type == 'Polygon':
                layer_id = feature['properties'].get('layerId')
                if layer_id is None:
                    continue

                if layer_id not in sub_layers:
                    sub_layer_name = f"Giraffe:{layer_id}"
                    sub_layer = rh.Layer()
                    sub_layer.Name = sub_layer_name
                    sub_layers[layer_id] = geometry_model.Layers.Add(
                        sub_layer)

                geometry = feature.get(
                    'geometry', {}).get('coordinates')
                height = feature.get(
                    'properties', {}).get('_height')
                base_height = feature.get(
                    'properties', {}).get('_baseHeight')
                usage = feature.get('properties', {}).get('usage')

                if base_height == 0 and height == 0:
                    for ring in geometry:
                        points = []
                        for coord in ring:
                            x, y = transformer2.transform(
                                coord[0], coord[1])
                            point = rh.Point3d(x, y, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        att = rh.ObjectAttributes()
                        att.SetUserString("Usage", str(usage))
                        att.LayerIndex = sub_layers[layer_id]
                        geometry_model.Objects.AddCurve(curve, att)

                for ring in geometry:
                    points = []
                    for coord in ring:
                        x, y = transformer2.transform(
                            coord[0], coord[1])
                        point = rh.Point3d(x, y, base_height)
                        points.append(point)

                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    extrusion_height = height - base_height
                    extrusion = rh.Extrusion.Create(
                        curve, extrusion_height, True)

                    att = rh.ObjectAttributes()
                    att.SetUserString("Usage", str(usage))
                    att.LayerIndex = sub_layers[layer_id]
                    geometry_model.Objects.AddExtrusion(
                        extrusion, att)
    else:
        pass

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in geometry_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "geometry.3dm"
    geometry_model.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/submit/elevated', methods=['POST'])
def get_elevated():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 20000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)
    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)
    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    elevated_model = rh.File3dm()
    elevated_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerEIndex = create_layer(
        elevated_model, "Boundary Elevated", (237, 0, 194, 255))
    building_layer_EIndex = create_layer(
        elevated_model, "Buildings Elevated", (99, 99, 99, 255))
    topography_layerIndex = create_layer(
        elevated_model, "Topography", (191, 191, 191, 255))
    contours_layer_EIndex = create_layer(
        elevated_model, "Contours Elevated", (191, 191, 191, 255))

    gh_topography_decoded = encode_ghx_file(
        r"./gh_scripts/topography.ghx")
    gh_buildings_elevated_decoded = encode_ghx_file(
        r"./gh_scripts/elevate_buildings.ghx")

    params_dict = {
        boundary_url: boundary_params,
        topo_url: topo_params
    }

    urls = [
        boundary_url,
        topo_url
    ]

    data_dict = {}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(
            get_data, url, params=params_dict[url]): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            data = future.result()
            if data is not None:
                if url == topo_url:
                    data_dict['topography_data'] = data
                elif url == boundary_url:
                    data_dict['boundary_data'] = data

    topography_data = data_dict.get('topography_data')
    boundary_data = data_dict.get('boundary_data')

    bound_curve = add_bound_curve_to_model(
        boundary_data, elevated_model, boundary_layerEIndex)

    buildings = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'building' in tiles1:
            building_layerMB = tiles1['building']

            tile1 = mercantile.Tile(tile.x, tile.y, 16)
            bbox = mercantile.bounds(tile1)
            lon1, lat1, lon2, lat2 = bbox

            for feature in building_layerMB['features']:
                geometry_type = feature['geometry']['type']
                height = feature['properties']['height']
                if geometry_type == 'Polygon':
                    geometry = feature['geometry']['coordinates']
                    for ring in geometry:
                        points = []
                        for coord in ring:
                            x_val, y_val = coord[0], coord[1]
                            x_prop = (x_val / 4096)
                            y_prop = (y_val / 4096)
                            lon_delta = lon2 - lon1
                            lat_delta = lat2 - lat1
                            lon_mapped = lon1 + \
                                (x_prop * lon_delta)
                            lat_mapped = lat1 + \
                                (y_prop * lat_delta)
                            lon_mapped, lat_mapped = transformer2.transform(
                                lon_mapped, lat_mapped)
                            point = rh.Point3d(
                                lon_mapped, lat_mapped, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        buildings.append(extrusion)
                elif geometry_type == 'MultiPolygon':
                    geometry = feature['geometry']['coordinates']
                    for polygon in geometry:
                        for ring in polygon:
                            points = []
                            for coord in ring:
                                x_val, y_val = coord[0], coord[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                            polyline = rh.Polyline(points)
                            curve = polyline.ToNurbsCurve()
                            orientation = curve.ClosedCurveOrientation()
                            if str(orientation) == 'CurveOrientation.Clockwise':
                                curve.Reverse()
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            buildings.append(extrusion)
        else:
            time.sleep(0)

    terrain_curves = []
    terrain_elevations = []
    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['elevation']
            geometry = feature["geometry"]

            for ring in geometry["paths"]:
                points = []
                points_e = []

                for coord in ring:
                    point = rh.Point3d(coord[0], coord[1], 0)
                    point_e = rh.Point3d(
                        coord[0], coord[1], elevation)
                    points.append(point)
                    points_e.append(point_e)

                polyline = rh.Polyline(points)
                polyline_e = rh.Polyline(points_e)
                curve = polyline.ToNurbsCurve()
                curve_e = polyline_e.ToNurbsCurve()

                terrain_curves.append(curve)
                terrain_elevations.append(int(elevation))

                att = rh.ObjectAttributes()
                att.LayerIndex = contours_layer_EIndex
                att.SetUserString("Elevation", str(elevation))
                elevated_model.Objects.AddCurve(curve_e, att)
    else:
        time.sleep(0)

    mesh_geo_list = []
    curves_list_terrain = [
        {"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(terrain_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_list_terrain[0]["InnerTree"][key] = value

    elevations_list_terrain = [
        {"ParamName": "Elevations", "InnerTree": {}}]
    for i, elevation in enumerate(terrain_elevations):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": elevation
            }
        ]
        elevations_list_terrain[0]["InnerTree"][key] = value

    centre_list = []
    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    centre_list.append(centroid)

    centre_point_list = [{"ParamName": "Point", "InnerTree": {}}]
    for i, point in enumerate(centre_list):
        serialized_point = json.dumps(point, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Point",
                "data": serialized_point
            }
        ]
        centre_point_list[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_topography_decoded,
        "pointer": None,
        "values": curves_list_terrain + elevations_list_terrain + centre_point_list
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        elif not res.ok:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
        time.sleep(0)

    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        innerTree = val['InnerTree']

        for key, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    mesh_geo = rh.CommonObject.Decode(data)
                    mesh_geo_list.append(mesh_geo)

                    att = rh.ObjectAttributes()
                    att.LayerIndex = topography_layerIndex
                    elevated_model.Objects.AddMesh(mesh_geo, att)

    buildings_elevated = [
        {"ParamName": "Buildings", "InnerTree": {}}]

    for i, brep in enumerate(buildings):
        serialized_extrusion = json.dumps(
            brep, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Extrusion",
                "data": serialized_extrusion
            }
        ]
        buildings_elevated[0]["InnerTree"][key] = value

    mesh_terrain = [{"ParamName": "Mesh", "InnerTree": {}}]
    for i, mesh in enumerate(mesh_geo_list):
        serialized = json.dumps(mesh, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Mesh",
                "data": serialized
            }
        ]
        mesh_terrain[0]["InnerTree"][key] = value

    boundcurves_list = []
    boundcurves_list.append(bound_curve)

    bound_curves = [{"ParamName": "Boundary", "InnerTree": {}}]
    for i, curve in enumerate(boundcurves_list):
        serialized = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized
            }
        ]
        bound_curves[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_buildings_elevated_decoded,
        "pointer": None,
        "values": buildings_elevated + mesh_terrain + bound_curves
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Elevated':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layer_EIndex
                        elevated_model.Objects.AddBrep(geo, att)
        elif paramName == 'RH_OUT:UpBound':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = boundary_layerEIndex
                        elevated_model.Objects.AddCurve(geo, att)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None: 
        bound_curve.Translate(translation_vector)

    for obj in elevated_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "elevated.3dm"
    elevated_model.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/planning', methods=['GET', 'POST'])
def planning():
    return render_template('planning.html', lat=-33.82267, lon=151.20124)


@application.route('/qld', methods=['GET', 'POST'])
def qld():
    return render_template('qld.html', lat=-27.462308, lon=153.028443)


@application.route('/vic', methods=['GET', 'POST'])
def vic():
    return render_template('vic.html', lat=-37.8212907, lon=144.9451695)

@application.route('/qld_planning', methods=['POST'])
def get_qld_planning():

    boundary_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/PlanningCadastre/LandParcelPropertyFramework/MapServer/8/query'

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    qld = rh.File3dm()
    qld.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(qld, "Boundary", (237, 0, 194, 255))
    admin_layerIndex = create_layer(
        qld, "Administrative Boundaries", (134, 69, 255, 255))
    native_layerIndex = create_layer(qld, "Native Land", (134, 69, 255, 255))
    zoning_layerIndex = create_layer(qld, "Zoning", (255, 180, 18, 255))
    lots_layerIndex = create_layer(qld, "Lots", (255, 106, 0, 255))
    road_layerIndex = create_layer(qld, "Roads", (145, 145, 145, 255))
    walking_layerIndex = create_layer(
        qld, "Walking Isochrone", (129, 168, 0, 255))
    cycling_layerIndex = create_layer(
        qld, "Cycling Isochrone", (0, 168, 168, 255))
    driving_layerIndex = create_layer(
        qld, "Driving Isochrone", (168, 0, 121, 255))
    bushfire_layerIndex = create_layer(qld, "Bushfire", (176, 7, 7, 255))
    flood_layerIndex = create_layer(qld, "Flood", (113, 173, 201, 255))
    heritage_layerIndex = create_layer(qld, "Heritage", (153, 153, 153, 255))
    overlandflow_layerIndex = create_layer(
        qld, "Overland Flow", (255, 106, 0, 255))
    creek_layerIndex = create_layer(
        qld, "Creek/Waterway Flood", (255, 106, 0, 255))
    river_layerIndex = create_layer(qld, "River Flood", (255, 106, 0, 255))

    gh_admin_decoded = encode_ghx_file(r"./gh_scripts/admin.ghx")
    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/zoning.ghx")
    gh_interpolate_decoded = encode_ghx_file(r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_bushfire_decoded = encode_ghx_file(r"./gh_scripts/bushfire.ghx")

    adminboundaries_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Boundaries/AdministrativeBoundaries/MapServer/2/query'
    zoning_url = "https://services2.arcgis.com/dEKgZETqwmDAh1rP/arcgis/rest/services/Zoning_opendata/FeatureServer/0/query"
    overflow_url = "https://services2.arcgis.com/dEKgZETqwmDAh1rP/arcgis/rest/services/Flood_overlay_Overland_flow/FeatureServer/0/query"
    creek_url = "https://services2.arcgis.com/dEKgZETqwmDAh1rP/arcgis/rest/services/Flood_overlay_Creek_waterway_flood_planning_area/FeatureServer/0/query"
    heritage_url = "https://services2.arcgis.com/dEKgZETqwmDAh1rP/arcgis/rest/services/Heritage_overlay_Area_adjoining/FeatureServer/0/query"
    bushfire_url = "https://services2.arcgis.com/dEKgZETqwmDAh1rP/arcgis/rest/services/Bushfire_overlay/FeatureServer/0/query"
    river_url = "https://services2.arcgis.com/dEKgZETqwmDAh1rP/arcgis/rest/services/Flood_overlay_Brisbane_River_flood_planning_area/FeatureServer/0/query"
    native_url = 'https://native-land.ca/wp-json/nativeland/v1/api/index.php'
    lots_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/PlanningCadastre/LandParcelPropertyFramework/MapServer/8/query'

    zoom = 16
    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 20000)
    r_xmin_LL, r_xmax_LL, r_ymin_LL, r_ymax_LL = create_boundary(
        lat, lon, 60000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)
    n_xmin_LL, n_xmax_LL, n_ymin_LL, n_ymax_LL = create_boundary(
        lat, lon, 800000)

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    r_params = {
        'where': '1=1',
        'geometry': f'{r_xmin_LL}, {r_ymin_LL},{r_xmax_LL},{r_ymax_LL}',
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelContains',
        'returnGeometry': 'true',
        'f': 'json',
        'outFields': '*',
        'inSR': '4326',
        'outSR': '32756',

    }

    native_post = {
        'maps': 'territories',
        'polygon_geojson': {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'properties': {},
                    'geometry': {
                        'type': 'Polygon',
                        'coordinates': [
                            [
                                [n_xmin_LL, n_ymin_LL],
                                [n_xmax_LL, n_ymin_LL],
                                [n_xmax_LL, n_ymax_LL],
                                [n_xmin_LL, n_ymax_LL],
                                [n_xmin_LL, n_ymin_LL]
                            ]
                        ]
                    }
                }
            ]
        }
    }

    params_dict = {
        adminboundaries_url: params,
        zoning_url: params,
        overflow_url: params,
        creek_url: params,
        heritage_url: params,
        bushfire_url: params,
        river_url: r_params,
        lots_url: params
    }

    urls = [
        adminboundaries_url,
        zoning_url,
        overflow_url,
        creek_url,
        heritage_url,
        bushfire_url,
        river_url,
        lots_url
    ]

    data_dict = {}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(
            get_data, url, params=params_dict[url]): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            data = future.result()
            if data is not None:
                if url == adminboundaries_url:
                    data_dict['admin_data'] = data
                elif url == zoning_url:
                    data_dict['zoning_data'] = data
                elif url == overflow_url:
                    data_dict['overflow_data'] = data
                elif url == creek_url:
                    data_dict['creek_data'] = data
                elif url == heritage_url:
                    data_dict['heritage_data'] = data
                elif url == bushfire_url:
                    data_dict['bushfire_data'] = data
                elif url == river_url:
                    data_dict['river_data'] = data
                elif url == lots_url:
                    data_dict['lots_data'] = data

    admin_data = data_dict.get('admin_data')
    zoning_data = data_dict.get('zoning_data')
    overflow_data = data_dict.get('overflow_data')
    creek_data = data_dict.get('creek_data')
    heritage_data = data_dict.get('heritage_data')
    bushfire_data = data_dict.get('bushfire_data')
    river_data = data_dict.get('river_data')
    lots_data = data_dict.get('lots_data')

    boundary_data = get_data(boundary_url, boundary_params)
    bound_curve = add_bound_curve_to_model(
        boundary_data, qld, boundary_layerIndex)

    counter = 0
    while True:
        native_response = requests.post(native_url, json=native_post)
        if native_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    native_data = native_response.json()
    for feature in native_data:
        geometry = feature['geometry']
        properties = feature['properties']
        name = properties['Name']
        for ring in geometry['coordinates']:
            points = []
            for coord in ring:
                native_x, native_y = transformer2.transform(
                    coord[0], coord[1])
                point = rh.Point3d(native_x, native_y, 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = native_layerIndex
            att.SetUserString("Native Name", str(name))
            qld.Objects.AddCurve(curve, att)

    admin_curves = []
    suburb_names = []
    if "features" in admin_data:
        for feature in admin_data["features"]:
            suburb_name = feature['attributes']['locality']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                admin_curves.append(curve)
                suburb_names.append(suburb_name)
    else:
        time.sleep(0)

    curves_admin = [{"ParamName": "Curves", "InnerTree": {}}]
    for i, curve in enumerate(admin_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_admin[0]["InnerTree"][key] = value

    names_admin = [{"ParamName": "Admin", "InnerTree": {}}]
    for i, admin in enumerate(suburb_names):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": admin
            }
        ]
        names_admin[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_admin_decoded,
        "pointer": None,
        "values": curves_admin + names_admin
    }

    admin_names_sorted = []
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:AdminBound':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        admin_names_sorted.append(data)

    i = 0
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = admin_layerIndex
                        att.SetUserString(
                            "Suburb", admin_names_sorted[i])
                        qld.Objects.AddMesh(geo, att)
                        i += 1

    zoning_curves = []
    zoning_names = []
    if "features" in zoning_data:
        for feature in zoning_data["features"]:
            zoning_code = feature['attributes']['ZONE_CODE']
            zoning_number = feature['attributes']['ZONE_PREC_NO']
            if zoning_number == None:
                zoning_number = ''
                zoning_name = str(
                    zoning_code) + str(zoning_number)
            else:
                zoning_name = str(
                    zoning_code) + str(zoning_number)
            geometry = feature["geometry"]
            points = []
            for coord in geometry["rings"][0]:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            zoning_curves.append(curve)
            zoning_names.append(zoning_name)
    else:
        time.sleep(0)

    curves_zoning = [{"ParamName": "Curves", "InnerTree": {}}]
    for i, curve in enumerate(zoning_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_zoning[0]["InnerTree"][key] = value

    names_zoning = [{"ParamName": "Zoning", "InnerTree": {}}]
    for i, zone in enumerate(zoning_names):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": zone
            }
        ]
        names_zoning[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_zoning_decoded,
        "pointer": None,
        "values": curves_zoning + names_zoning
    }

    zoning_names_sorted = []
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Zone':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        zoning_names_sorted.append(data)

    i = 0
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = zoning_layerIndex
                        att.SetUserString(
                            "Zoning Code", zoning_names_sorted[i])
                        qld.Objects.AddMesh(geo, att)
                        i += 1

    add_to_model(lots_data, lots_layerIndex, "lotplan", "Lot Number", qld)

    add_to_model(overflow_data, overlandflow_layerIndex,
                 "OVL2_CAT", "Overland Flow Class", qld)

    add_to_model(creek_data, creek_layerIndex, "OVL2_CAT", "Creeks Class", qld)

    river_curves = []
    if "features" in river_data:
        for feature in river_data["features"]:
            ovl_cat = feature['attributes']['OVL2_DESC']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                river_curves.append(curve)
                att = rh.ObjectAttributes()
                att.LayerIndex = river_layerIndex
                att.SetUserString("Category", str(ovl_cat))
                qld.Objects.AddCurve(curve, att)
    else:
        time.sleep(0)

    add_to_model(heritage_data, heritage_layerIndex,
                 "OVL2_DESC", "Heritage Name", qld)

    road_curves = []
    for tile in tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{zoom}/{tile.x}/{tile.y}.mvt?access_token={mapbox_access_token}"
        counter = 0
        while True:
            mb_response = requests.get(mb_url)
            if mb_response.status_code == 200:
                break
            else:
                counter += 1
                if counter >= 3:
                    return jsonify({'error': True})
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)
        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            road_class = feature['properties']['class']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                points = []
                for ring in geometry:
                    x_val, y_val = ring[0], ring[1]
                    x_prop = (x_val / 4096)
                    y_prop = (y_val / 4096)
                    lon_delta = lon2 - lon1
                    lat_delta = lat2 - lat1
                    lon_mapped = lon1 + (x_prop * lon_delta)
                    lat_mapped = lat1 + (y_prop * lat_delta)
                    lon_mapped, lat_mapped = transformer2.transform(
                        lon_mapped, lat_mapped)
                    point = rh.Point3d(lon_mapped, lat_mapped, 0)
                    points.append(point)

                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                road_curves.append(curve)

            elif geometry_type == 'MultiLineString':
                geometry = feature['geometry']['coordinates']
                for line_string in geometry:
                    points = []
                    for ring in line_string:
                        x_val, y_val = ring[0], ring[1]
                        x_prop = (x_val / 4096)
                        y_prop = (y_val / 4096)
                        lon_delta = lon2 - lon1
                        lat_delta = lat2 - lat1
                        lon_mapped = lon1 + (x_prop * lon_delta)
                        lat_mapped = lat1 + (y_prop * lat_delta)
                        lon_mapped, lat_mapped = transformer2.transform(
                            lon_mapped, lat_mapped)
                        point = rh.Point3d(
                            lon_mapped, lat_mapped, 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    road_curves.append(curve)

    curves_list_roads = [{"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(road_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_list_roads[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_roads_decoded,
        "pointer": None,
        "values": curves_list_roads
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Roads':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = road_layerIndex
                        qld.Objects.AddCurve(geo, att)

    profile1 = 'mapbox/walking'
    profile2 = 'mapbox/cycling'
    profile3 = 'mapbox/driving'
    longitude_iso = lon
    latitude_iso = lat
    time_iso = '15'

    iso_url_w = f'https://api.mapbox.com/isochrone/v1/{profile1}/{longitude_iso},{latitude_iso}?contours_minutes={time_iso}&polygons=true&access_token={mapbox_access_token}'

    iso_url_c = f'https://api.mapbox.com/isochrone/v1/{profile2}/{longitude_iso},{latitude_iso}?contours_minutes={time_iso}&polygons=true&access_token={mapbox_access_token}'

    iso_url_d = f'https://api.mapbox.com/isochrone/v1/{profile3}/{longitude_iso},{latitude_iso}?contours_minutes={time_iso}&polygons=true&access_token={mapbox_access_token}'

    counter = 0
    while True:
        iso_response_w = requests.get(iso_url_w)
        if iso_response_w.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    walking_data = json.loads(iso_response_w.content.decode())

    counter = 0
    while True:
        iso_response_c = requests.get(iso_url_c)
        if iso_response_c.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    cycling_data = json.loads(iso_response_c.content.decode())

    counter = 0
    while True:
        iso_response_d = requests.get(iso_url_d)
        if iso_response_d.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    driving_data = json.loads(iso_response_d.content.decode())

    def add_curves_to_model(data, transformer, layerIndex, model):
        curves = []
        for feature in data['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'Polygon':
                geometry = feature['geometry']['coordinates']
                for ring in geometry:
                    points = []
                    for coord in ring:
                        iso_x, iso_y = coord[0], coord[1]
                        iso_x, iso_y = transformer.transform(iso_x, iso_y)
                        point = rh.Point3d(iso_x, iso_y, 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    curves.append(curve)

    add_curves_to_model(walking_data, transformer2, walking_layerIndex, qld)
    add_curves_to_model(cycling_data, transformer2, cycling_layerIndex, qld)
    add_curves_to_model(driving_data, transformer2, driving_layerIndex, qld)

    bushfire_curves = []
    bushfire_numbers = []
    counter = 0
    while True:
        bushfire_response = requests.get(
            bushfire_url, params=params)
        if bushfire_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    bushfire_data = json.loads(bushfire_response.text)
    if "features" in bushfire_data:
        for feature in bushfire_data["features"]:
            bushfire_class = feature['attributes']['OVL2_CAT']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                bushfire_curves.append(curve)
                bushfire_numbers.append(
                    str(bushfire_class))
    else:
        time.sleep(0)

    curves_bushfire = [{"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(bushfire_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_bushfire[0]["InnerTree"][key] = value

    names_bushfire = [{"ParamName": "Bushfire", "InnerTree": {}}]
    for i, bushfire in enumerate(bushfire_numbers):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": bushfire
            }
        ]
        names_bushfire[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_bushfire_decoded,
        "pointer": None,
        "values": curves_bushfire + names_bushfire
    }

    bushfire_names_sorted = []
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:ClassBF':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        bushfire_names_sorted.append(data)

    i = 0
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = bushfire_layerIndex
                        att.SetUserString("Bushfire Class",
                                          bushfire_names_sorted[i])
                        qld.Objects.AddMesh(geo, att)
                        i += 1

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in qld.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "qld_planning.3dm"
    qld.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/qld_geometry', methods=['POST'])
def get_qld_geometry():

    boundary_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/PlanningCadastre/LandParcelPropertyFramework/MapServer/8/query'
    topo_url = "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Elevation/ContoursCache/MapServer/0/query"

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    qld_g = rh.File3dm()
    qld_g.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(qld_g, "Boundary", (237, 0, 194, 255))
    building_layerIndex = create_layer(qld_g, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(qld_g, "Contours", (191, 191, 191, 255))
    geometry_layerIndex = create_layer(qld_g, "Geometry", (191, 191, 191, 255))

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 20000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    params_dict = {
        boundary_url: boundary_params,
        topo_url: topo_params
    }

    urls = [
        boundary_url,
        topo_url
    ]

    data_dict = {}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(
            get_data, url, params=params_dict[url]): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            data = future.result()
            if data is not None:
                if url == topo_url:
                    data_dict['topography_data'] = data
                elif url == boundary_url:
                    data_dict['boundary_data'] = data

    topography_data = data_dict.get('topography_data')
    boundary_data = data_dict.get('boundary_data')

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    bound_curve = add_bound_curve_to_model(
        boundary_data, qld_g, boundary_layerIndex)

    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'building' in tiles1:
            building_layerMB = tiles1['building']

            tile1 = mercantile.Tile(tile.x, tile.y, 16)
            bbox = mercantile.bounds(tile1)
            lon1, lat1, lon2, lat2 = bbox

            for feature in building_layerMB['features']:
                geometry_type = feature['geometry']['type']
                height = feature['properties']['height']
                if geometry_type == 'Polygon':
                    geometry = feature['geometry']['coordinates']
                    for ring in geometry:
                        points = []
                        for coord in ring:
                            x_val, y_val = coord[0], coord[1]
                            x_prop = (x_val / 4096)
                            y_prop = (y_val / 4096)
                            lon_delta = lon2 - lon1
                            lat_delta = lat2 - lat1
                            lon_mapped = lon1 + (x_prop * lon_delta)
                            lat_mapped = lat1 + (y_prop * lat_delta)
                            lon_mapped, lat_mapped = transformer2.transform(
                                lon_mapped, lat_mapped)
                            point = rh.Point3d(
                                lon_mapped, lat_mapped, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        qld_g.Objects.AddExtrusion(
                            extrusion, att)
                elif geometry_type == 'MultiPolygon':
                    geometry = feature['geometry']['coordinates']
                    for polygon in geometry:
                        for ring in polygon:
                            points = []
                            for coord in ring:
                                x_val, y_val = coord[0], coord[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                            polyline = rh.Polyline(points)
                            curve = polyline.ToNurbsCurve()
                            orientation = curve.ClosedCurveOrientation()
                            if str(orientation) == 'CurveOrientation.Clockwise':
                                curve.Reverse()
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            qld_g.Objects.AddExtrusion(
                                extrusion, att)
        else:
            time.sleep(0)

    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['elevation']
            geometry = feature["geometry"]
            for ring in geometry["paths"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                att = rh.ObjectAttributes()
                att.LayerIndex = contours_layerIndex
                att.SetUserString(
                    "Elevation", str(elevation))
                qld_g.Objects.AddCurve(curve, att)
    else:
        time.sleep(0)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in qld_g.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "qld_geometry.3dm"
    qld_g.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/qld_elevated', methods=['POST'])
def get_qld_elevated():

    boundary_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/PlanningCadastre/LandParcelPropertyFramework/MapServer/8/query'
    topo_url = "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Elevation/ContoursCache/MapServer/0/query"

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    qld_e = rh.File3dm()
    qld_e.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerEIndex = create_layer(qld_e, "Boundary Elevated", (237, 0, 194, 255))
    building_layer_EIndex = create_layer(
        qld_e, "Buildings Elevated", (99, 99, 99, 255))
    topography_layerIndex = create_layer(
        qld_e, "Topography", (191, 191, 191, 255))
    contours_layer_EIndex = create_layer(
        qld_e, "Contours Elevated", (191, 191, 191, 255))

    gh_topography_decoded = encode_ghx_file(r"./gh_scripts/topography.ghx")
    gh_buildings_elevated_decoded = encode_ghx_file(
        r"./gh_scripts/elevate_buildings.ghx")

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 20000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    params_dict = {
        boundary_url: boundary_params,
        topo_url: topo_params
    }

    urls = [
        boundary_url,
        topo_url
    ]

    data_dict = {}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_url = {executor.submit(
            get_data, url, params=params_dict[url]): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            data = future.result()
            if data is not None:
                if url == topo_url:
                    data_dict['topography_data'] = data
                elif url == boundary_url:
                    data_dict['boundary_data'] = data

    topography_data = data_dict.get('topography_data')
    boundary_data = data_dict.get('boundary_data')

    bound_curve = add_bound_curve_to_model(
        boundary_data, qld_e, boundary_layerEIndex)

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    buildings = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'building' in tiles1:
            building_layerMB = tiles1['building']

            tile1 = mercantile.Tile(tile.x, tile.y, 16)
            bbox = mercantile.bounds(tile1)
            lon1, lat1, lon2, lat2 = bbox

            for feature in building_layerMB['features']:
                geometry_type = feature['geometry']['type']
                height = feature['properties']['height']
                if geometry_type == 'Polygon':
                    geometry = feature['geometry']['coordinates']
                    for ring in geometry:
                        points = []
                        for coord in ring:
                            x_val, y_val = coord[0], coord[1]
                            x_prop = (x_val / 4096)
                            y_prop = (y_val / 4096)
                            lon_delta = lon2 - lon1
                            lat_delta = lat2 - lat1
                            lon_mapped = lon1 + (x_prop * lon_delta)
                            lat_mapped = lat1 + (y_prop * lat_delta)
                            lon_mapped, lat_mapped = transformer2.transform(
                                lon_mapped, lat_mapped)
                            point = rh.Point3d(
                                lon_mapped, lat_mapped, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        buildings.append(extrusion)
                elif geometry_type == 'MultiPolygon':
                    geometry = feature['geometry']['coordinates']
                    for polygon in geometry:
                        for ring in polygon:
                            points = []
                            for coord in ring:
                                x_val, y_val = coord[0], coord[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                            polyline = rh.Polyline(points)
                            curve = polyline.ToNurbsCurve()
                            orientation = curve.ClosedCurveOrientation()
                            if str(orientation) == 'CurveOrientation.Clockwise':
                                curve.Reverse()
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            buildings.append(extrusion)
        else:
            time.sleep(0)

    terrain_curves = []
    terrain_elevations = []
    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['elevation']
            geometry = feature["geometry"]
            for ring in geometry["paths"]:
                points = []
                points_e = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    point_e = rh.Point3d(
                        coord[0], coord[1], elevation)
                    points.append(point)
                    points_e.append(point_e)
                polyline = rh.Polyline(points)
                polyline_e = rh.Polyline(points_e)
                curve = polyline.ToNurbsCurve()
                curve_e = polyline_e.ToNurbsCurve()
                terrain_curves.append(curve)
                terrain_elevations.append(int(elevation))
                att = rh.ObjectAttributes()
                att.LayerIndex = contours_layer_EIndex
                att.SetUserString("Elevation", str(elevation))
                qld_e.Objects.AddCurve(
                    curve_e, att)
    else:
        time.sleep(0)

    mesh_geo_list = []
    curves_list_terrain = [
        {"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(terrain_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_list_terrain[0]["InnerTree"][key] = value

    elevations_list_terrain = [
        {"ParamName": "Elevations", "InnerTree": {}}]
    for i, elevation in enumerate(terrain_elevations):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": elevation
            }
        ]
        elevations_list_terrain[0]["InnerTree"][key] = value

    centre_list = []
    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    centre_list.append(centroid)

    centre_point_list = [{"ParamName": "Point", "InnerTree": {}}]
    for i, point in enumerate(centre_list):
        serialized_point = json.dumps(point, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Point",
                "data": serialized_point
            }
        ]
        centre_point_list[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_topography_decoded,
        "pointer": None,
        "values": curves_list_terrain + elevations_list_terrain + centre_point_list
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        innerTree = val['InnerTree']
        for key, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    mesh_geo = rh.CommonObject.Decode(data)
                    mesh_geo_list.append(mesh_geo)
                    att = rh.ObjectAttributes()
                    att.LayerIndex = topography_layerIndex
                    qld_e.Objects.AddMesh(mesh_geo, att)

    buildings_elevated = [
        {"ParamName": "Buildings", "InnerTree": {}}]

    for i, brep in enumerate(buildings):
        serialized_extrusion = json.dumps(
            brep, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Extrusion",
                "data": serialized_extrusion
            }
        ]
        buildings_elevated[0]["InnerTree"][key] = value

    mesh_terrain = [{"ParamName": "Mesh", "InnerTree": {}}]
    for i, mesh in enumerate(mesh_geo_list):
        serialized = json.dumps(mesh, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Mesh",
                "data": serialized
            }
        ]
        mesh_terrain[0]["InnerTree"][key] = value

    boundcurves_list = []
    boundcurves_list.append(bound_curve)

    bound_curves = [{"ParamName": "Boundary", "InnerTree": {}}]
    for i, curve in enumerate(boundcurves_list):
        serialized = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized
            }
        ]
        bound_curves[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_buildings_elevated_decoded,
        "pointer": None,
        "values": buildings_elevated + mesh_terrain + bound_curves
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Elevated':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layer_EIndex
                        qld_e.Objects.AddBrep(geo, att)
        elif paramName == 'RH_OUT:UpBound':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = boundary_layerEIndex
                        qld_e.Objects.AddCurve(geo, att)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:
        bound_curve.Translate(translation_vector)

    for obj in qld_e.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "qld_elevated.3dm"
    qld_e.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/vic_planning', methods=['POST'])
def get_vic_planning():

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    vic = rh.File3dm()
    vic.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/V_PARCEL_MP/MapServer/0/query'
    zoning_url = 'https://plan-gis.mapshare.vic.gov.au/arcgis/rest/services/Planning/Vicplan_PlanningSchemeZones/MapServer/0/query'
    adminboundaries_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/Vicmap_Admin/MapServer/9/query'
    bushfire_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/Zones_and_Overlays/MapServer/16/query'
    vegetation_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/Zones_and_Overlays/MapServer/4/query'
    flood_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/Zones_and_Overlays/MapServer/11/query'
    heritage_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/Zones_and_Overlays/MapServer/7/query'
    native_url = 'https://native-land.ca/wp-json/nativeland/v1/api/index.php'

    gh_admin_decoded = encode_ghx_file(r"./gh_scripts/admin.ghx")
    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/zoning.ghx")
    gh_interpolate_decoded = encode_ghx_file(r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_lots_decoded = encode_ghx_file(r"./gh_scripts/vic_lots.ghx")

    boundary_layerIndex = create_layer(vic, "Boundary", (237, 0, 194, 255))
    admin_layerIndex = create_layer(
        vic, "Administrative Boundaries", (134, 69, 255, 255))
    native_layerIndex = create_layer(vic, "Native Land", (134, 69, 255, 255))
    zoning_layerIndex = create_layer(vic, "Zoning", (255, 180, 18, 255))
    lots_layerIndex = create_layer(vic, "Lots", (255, 106, 0, 255))
    road_layerIndex = create_layer(vic, "Roads", (145, 145, 145, 255))
    walking_layerIndex = create_layer(
        vic, "Walking Isochrone", (129, 168, 0, 255))
    cycling_layerIndex = create_layer(
        vic, "Cycling Isochrone", (0, 168, 168, 255))
    driving_layerIndex = create_layer(
        vic, "Driving Isochrone", (168, 0, 121, 255))
    bushfire_layerIndex = create_layer(vic, "Bushfire", (176, 7, 7, 255))
    flood_layerIndex = create_layer(vic, "Flood", (113, 173, 201, 255))
    heritage_layerIndex = create_layer(vic, "Heritage", (153, 153, 153, 255))
    vegetation_layerIndex = create_layer(
        vic, "Vegetation", (153, 153, 153, 255))
    
    l_xmin_LL, l_xmax_LL, l_ymin_LL, l_ymax_LL = create_boundary(lat, lon, 15000)
    n_xmin_LL, n_xmax_LL, n_ymin_LL, n_ymax_LL = create_boundary(lat, lon, 800000)

    l_params = {
        'where': '1=1',
        'geometry': f'{l_xmin_LL}, {l_ymin_LL},{l_xmax_LL},{l_ymax_LL}',
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelContains',
        'returnGeometry': 'true',
        'f': 'json',
        'outFields': '*',
        'inSR': '4326',
        'outSR': '32755',
    }

    native_post = {
        'maps': 'territories',
        'polygon_geojson': {
            'type': 'FeatureCollection',
            'features': [
                {
                    'type': 'Feature',
                    'properties': {},
                    'geometry': {
                        'type': 'Polygon',
                        'coordinates': [
                            [
                                [n_xmin_LL, n_ymin_LL],
                                [n_xmax_LL, n_ymin_LL],
                                [n_xmax_LL, n_ymax_LL],
                                [n_xmin_LL, n_ymax_LL],
                                [n_xmin_LL, n_ymin_LL]
                            ]
                        ]
                    }
                }
            ]
        }
    }

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 30000)
    l_xmin_LL, l_xmax_LL, l_ymin_LL, l_ymax_LL = create_boundary(
        lat, lon, 15000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 200000)
    n_xmin_LL, n_xmax_LL, n_ymin_LL, n_ymax_LL = create_boundary(
        lat, lon, 800000)

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)

    boundary_data = get_data(
        boundary_url, boundary_params)
    bound_curve = add_bound_curve_to_model(boundary_data, vic)

    counter = 0
    while True:
        native_response = requests.post(native_url, json=native_post)
        if native_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    native_data = native_response.json()
    for feature in native_data:
        geometry = feature['geometry']
        properties = feature['properties']
        name = properties['Name']
        for ring in geometry['coordinates']:
            points = []
            for coord in ring:
                native_x, native_y = transformer2.transform(
                    coord[0], coord[1])
                point = rh.Point3d(native_x, native_y, 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = native_layerIndex
            att.SetUserString("Native Name", str(name))
            vic.Objects.AddCurve(curve, att)

    admin_curves = []
    suburb_names = []
    counter = 0
    while True:
        admin_response = requests.get(adminboundaries_url, params=params)
        if admin_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    admin_data = json.loads(admin_response.text)
    if "features" in admin_data:
        for feature in admin_data["features"]:
            suburb_name = feature['attributes']['lga_name']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                admin_curves.append(curve)
                suburb_names.append(suburb_name)
    else:
        time.sleep(0)

    curves_admin = [{"ParamName": "Curves", "InnerTree": {}}]
    for i, curve in enumerate(admin_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_admin[0]["InnerTree"][key] = value

    names_admin = [{"ParamName": "Admin", "InnerTree": {}}]
    for i, admin in enumerate(suburb_names):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": admin
            }
        ]
        names_admin[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_admin_decoded,
        "pointer": None,
        "values": curves_admin + names_admin
    }

    admin_names_sorted = []
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:AdminBound':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        admin_names_sorted.append(data)

    i = 0
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = admin_layerIndex
                        att.SetUserString(
                            "Suburb", admin_names_sorted[i])
                        vic.Objects.AddMesh(geo, att)
                        i += 1

    zoning_curves = []
    zoning_names = []
    counter = 0
    while True:
        zoning_response = requests.get(zoning_url, params=params)
        if zoning_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    zoning_data = json.loads(zoning_response.text)
    if "features" in zoning_data:
        for feature in zoning_data["features"]:
            zoning_code = feature['attributes']['ZONE_CODE']
            geometry = feature["geometry"]
            points = []
            for coord in geometry["rings"][0]:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            zoning_curves.append(curve)
            zoning_names.append(zoning_code)
    else:
        time.sleep(0)

    curves_zoning = [{"ParamName": "Curves", "InnerTree": {}}]
    for i, curve in enumerate(zoning_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_zoning[0]["InnerTree"][key] = value

    names_zoning = [{"ParamName": "Zoning", "InnerTree": {}}]
    for i, zone in enumerate(zoning_names):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": zone
            }
        ]
        names_zoning[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_zoning_decoded,
        "pointer": None,
        "values": curves_zoning + names_zoning
    }

    zoning_names_sorted = []
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Zone':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        zoning_names_sorted.append(data)

    i = 0
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = zoning_layerIndex
                        att.SetUserString(
                            "Zoning Code", zoning_names_sorted[i])
                        vic.Objects.AddMesh(geo, att)
                        i += 1

    lots_curves = []
    counter = 0
    while True:
        lots_response = requests.get(boundary_url, params=l_params)
        if lots_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    lots_data = json.loads(lots_response.text)
    if "features" in lots_data:
        for feature in lots_data["features"]:
            lot_number = feature['attributes']['PARCEL_SPI']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                lots_curves.append(curve)
    else:
        time.sleep(0)

    curves_lots = [{"ParamName": "Curves", "InnerTree": {}}]
    for i, curve in enumerate(lots_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_lots[0]["InnerTree"][key] = value
    geo_payload = {
        "algo": gh_lots_decoded,
        "pointer": None,
        "values": curves_lots
    }
    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Lots':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = lots_layerIndex
                        vic.Objects.AddCurve(geo, att)

    bushfire_data = get_data(bushfire_url, params)
    add_to_model(bushfire_data, bushfire_layerIndex,
                 "ZONE_CODE", "Bushfire Class", vic)

    flood_data = get_data(flood_url, params)
    add_to_model(flood_data, flood_layerIndex, "ZONE_CODE", "Flood Class", vic)

    heritage_data = get_data(heritage_url, params)
    add_to_model(heritage_data, heritage_layerIndex,
                 "ZONE_CODE", "Heritage Name", vic)

    vegetation_data = get_data(vegetation_url, params)
    add_to_model(vegetation_data, vegetation_layerIndex,
                 "ZONE_CODE", "Vegetation Code", vic)

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    road_curves = []
    for tile in tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{zoom}/{tile.x}/{tile.y}.mvt?access_token={mapbox_access_token}"
        counter = 0
        while True:
            mb_response = requests.get(mb_url)
            if mb_response.status_code == 200:
                break
            else:
                counter += 1
                if counter >= 3:
                    return jsonify({'error': True})
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)
        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            road_class = feature['properties']['class']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                points = []
                for ring in geometry:
                    x_val, y_val = ring[0], ring[1]
                    x_prop = (x_val / 4096)
                    y_prop = (y_val / 4096)
                    lon_delta = lon2 - lon1
                    lat_delta = lat2 - lat1
                    lon_mapped = lon1 + (x_prop * lon_delta)
                    lat_mapped = lat1 + (y_prop * lat_delta)
                    lon_mapped, lat_mapped = transformer2.transform(
                        lon_mapped, lat_mapped)
                    point = rh.Point3d(lon_mapped, lat_mapped, 0)
                    points.append(point)

                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                road_curves.append(curve)

            elif geometry_type == 'MultiLineString':
                geometry = feature['geometry']['coordinates']
                for line_string in geometry:
                    points = []
                    for ring in line_string:
                        x_val, y_val = ring[0], ring[1]
                        x_prop = (x_val / 4096)
                        y_prop = (y_val / 4096)
                        lon_delta = lon2 - lon1
                        lat_delta = lat2 - lat1
                        lon_mapped = lon1 + (x_prop * lon_delta)
                        lat_mapped = lat1 + (y_prop * lat_delta)
                        lon_mapped, lat_mapped = transformer2.transform(
                            lon_mapped, lat_mapped)
                        point = rh.Point3d(
                            lon_mapped, lat_mapped, 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    road_curves.append(curve)

    curves_list_roads = [{"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(road_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_list_roads[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_roads_decoded,
        "pointer": None,
        "values": curves_list_roads
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Roads':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = road_layerIndex
                        vic.Objects.AddCurve(geo, att)

    profile1 = 'mapbox/walking'
    profile2 = 'mapbox/cycling'
    profile3 = 'mapbox/driving'
    longitude_iso = lon
    latitude_iso = lat
    iso_url_w = f'https://api.mapbox.com/isochrone/v1/{profile1}/{longitude_iso},{latitude_iso}?contours_minutes=5&polygons=true&access_token={mapbox_access_token}'
    iso_url_c = f'https://api.mapbox.com/isochrone/v1/{profile2}/{longitude_iso},{latitude_iso}?contours_minutes=10&polygons=true&access_token={mapbox_access_token}'
    iso_url_d = f'https://api.mapbox.com/isochrone/v1/{profile3}/{longitude_iso},{latitude_iso}?contours_minutes=15&polygons=true&access_token={mapbox_access_token}'

    counter = 0
    while True:
        iso_response_w = requests.get(iso_url_w)
        if iso_response_w.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    walking_data = json.loads(iso_response_w.content.decode())

    counter = 0
    while True:
        iso_response_c = requests.get(iso_url_c)
        if iso_response_c.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    cycling_data = json.loads(iso_response_c.content.decode())

    counter = 0
    while True:
        iso_response_d = requests.get(iso_url_d)
        if iso_response_d.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    driving_data = json.loads(iso_response_d.content.decode())

    def add_curves_to_model(data, transformer, layerIndex, model):
        curves = []
        for feature in data['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'Polygon':
                geometry = feature['geometry']['coordinates']
                for ring in geometry:
                    points = []
                    for coord in ring:
                        iso_x, iso_y = coord[0], coord[1]
                        iso_x, iso_y = transformer.transform(iso_x, iso_y)
                        point = rh.Point3d(iso_x, iso_y, 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    curves.append(curve)

        curves_data = [{"ParamName": "Curves", "InnerTree": {}}]
        for i, curve in enumerate(curves):
            serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "Rhino.Geometry.Curve",
                    "data": serialized_curve
                }
            ]
            curves_data[0]["InnerTree"][key] = value

        geo_payload = {
            "algo": gh_interpolate_decoded,
            "pointer": None,
            "values": curves_data
        }
        counter = 0
        while True:
            res = requests.post(compute_url + "grasshopper",
                                json=geo_payload, headers=headers)
            if res.status_code == 200:
                break
            else:
                counter += 1
                if counter >= 3:
                    return jsonify({'error': True})
        response_object = json.loads(res.content)['values']
        for val in response_object:
            paramName = val['ParamName']
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = layerIndex
                        model.Objects.AddCurve(geo, att)

    add_curves_to_model(walking_data, transformer2, walking_layerIndex, vic)
    add_curves_to_model(cycling_data, transformer2, cycling_layerIndex, vic)
    add_curves_to_model(driving_data, transformer2, driving_layerIndex, vic)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in vic.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "vic_planning.3dm"
    vic.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/vic_geometry', methods=['POST'])
def get_vic_geometry():

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    vic_g = rh.File3dm()
    vic_g.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/V_PARCEL_MP/MapServer/0/query'
    metro_topo_url = "https://services6.arcgis.com/GB33F62SbDxJjwEL/ArcGIS/rest/services/Vicmap_Elevation_METRO_1_to_5_metre/FeatureServer/1/query"
    regional_topo_url = "https://enterprise.mapshare.vic.gov.au/server/rest/services/Vicmap_Elevation_STATEWIDE_10_to_20_metre/MapServer/6/query"

    boundary_layerIndex = create_layer(vic_g, "Boundary", (237, 0, 194, 255))
    building_layerIndex = create_layer(vic_g, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(vic_g, "Contours", (191, 191, 191, 255))
    geometry_layerIndex = create_layer(vic_g, "Geometry", (191, 191, 191, 255))

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 30000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 200000)

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    boundary_data = get_data(boundary_url, boundary_params)
    bound_curve = add_bound_curve_to_model(
        boundary_data, vic_g, boundary_layerIndex)

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    for tile in tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{zoom}/{tile.x}/{tile.y}.mvt?access_token={mapbox_access_token}"
        counter = 0
        while True:
            mb_response = requests.get(mb_url)
            if mb_response.status_code == 200:
                break
            else:
                counter += 1
                if counter >= 3:
                    return jsonify({'error': True})
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'building' in tiles1:
            building_layerMB = tiles1['building']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in building_layerMB['features']:
            geometry_type = feature['geometry']['type']
            height = feature['properties']['height']
            if geometry_type == 'Polygon':
                geometry = feature['geometry']['coordinates']
                for ring in geometry:
                    points = []
                    for coord in ring:
                        x_val, y_val = coord[0], coord[1]
                        x_prop = (x_val / 4096)
                        y_prop = (y_val / 4096)
                        lon_delta = lon2 - lon1
                        lat_delta = lat2 - lat1
                        lon_mapped = lon1 + (x_prop * lon_delta)
                        lat_mapped = lat1 + (y_prop * lat_delta)
                        lon_mapped, lat_mapped = transformer2.transform(
                            lon_mapped, lat_mapped)
                        point = rh.Point3d(
                            lon_mapped, lat_mapped, 0)
                        points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                orientation = curve.ClosedCurveOrientation()
                if str(orientation) == 'CurveOrientation.Clockwise':
                    curve.Reverse()
                extrusion = rh.Extrusion.Create(
                    curve, height, True)
                att = rh.ObjectAttributes()
                att.SetUserString("Building Height", str(height))
                att.LayerIndex = building_layerIndex
                vic_g.Objects.AddExtrusion(extrusion, att)
            elif geometry_type == 'MultiPolygon':
                geometry = feature['geometry']['coordinates']
                for polygon in geometry:
                    for ring in polygon:
                        points = []
                        if isinstance(ring[0], list):
                            for subring in ring:
                                x_val, y_val = subring[0], subring[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                        else:
                            for coord in ring:
                                x_val, y_val = coord[0], coord[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        att = rh.ObjectAttributes()
                        att.SetUserString(
                            "Building Height", str(height))
                        att.LayerIndex = building_layerIndex
                        vic_g.Objects.AddExtrusion(extrusion, att)

    # if regional_toggle == 'Regional':
    #     topo_url = regional_topo_url
    # elif regional_toggle == 'Metro':
    topo_url = metro_topo_url

    counter = 0
    while True:
        topography_response = requests.get(topo_url, params=t_params)
        if topography_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    topography_data = json.loads(topography_response.text)
    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['altitude']
            geometry = feature["geometry"]
            for ring in geometry["paths"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                att = rh.ObjectAttributes()
                att.LayerIndex = contours_layerIndex
                att.SetUserString("Elevation", str(elevation))
                vic_g.Objects.AddCurve(curve, att)
    else:
        time.sleep(0)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in vic_g.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "vic_geometry.3dm"
    vic_g.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/vic_elevated', methods=['POST'])
def get_vic_elevated():

    address = request.form.get('address')
    endpoint = "https://api.mapbox.com/geocoding/v5/mapbox.places/{address}.json"
    params = {
        "access_token": mapbox_access_token,
        "autocomplete": False,
        "limit": 1,
        "query": address,
    }

    response = requests.get(endpoint.format(address=address), params=params)
    if response.status_code == 200:
        result = response.json()["features"][0]
        longitude, latitude = result["center"]
        lon = float(longitude)
        lat = float(latitude)
    else:
        return jsonify({'error': True})

    boundary_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/V_PARCEL_MP/MapServer/0/query'
    metro_topo_url = "https://services6.arcgis.com/GB33F62SbDxJjwEL/ArcGIS/rest/services/Vicmap_Elevation_METRO_1_to_5_metre/FeatureServer/1/query"
    regional_topo_url = "https://enterprise.mapshare.vic.gov.au/server/rest/services/Vicmap_Elevation_STATEWIDE_10_to_20_metre/MapServer/6/query"

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 30000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 200000)

    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_params = create_parameters(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    boundary_data = get_data(boundary_url, boundary_params)
    bound_curve = add_bound_curve_to_model(
        boundary_data, vic_e, boundary_layerIndex)

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    vic_e = rh.File3dm()
    vic_e.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(vic_e, "Boundary", (237, 0, 194, 255))
    building_layer_EIndex = create_layer(
        vic_e, "Buildings Elevated", (99, 99, 99, 255))
    topography_layerIndex = create_layer(
        vic_e, "Topography", (191, 191, 191, 255))
    contours_layer_EIndex = create_layer(
        vic_e, "Contours Elevated", (191, 191, 191, 255))

    gh_topography_decoded = encode_ghx_file(r"./gh_scripts/topography.ghx")
    gh_buildings_elevated_decoded = encode_ghx_file(
        r"./gh_scripts/elevate_buildings.ghx")

    bound_curves_list = []
    boundary_data = get_data(boundary_url, boundary_params)
    bound_curve = add_bound_curve_to_model(
        boundary_data, vic_e, boundary_layerIndex)
    bound_curves_list.append(bound_curve)

    buildings = []
    for tile in tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{zoom}/{tile.x}/{tile.y}.mvt?access_token={mapbox_access_token}"

        counter = 0
        while True:
            mb_response = requests.get(mb_url)
            if mb_response.status_code == 200:
                break
            else:
                counter += 1
                if counter >= 3:
                    return jsonify({'error': True})
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'building' in tiles1:
            building_layerMB = tiles1['building']

            tile1 = mercantile.Tile(tile.x, tile.y, 16)
            bbox = mercantile.bounds(tile1)
            lon1, lat1, lon2, lat2 = bbox

            for feature in building_layerMB['features']:
                geometry_type = feature['geometry']['type']
                height = feature['properties']['height']
                if geometry_type == 'Polygon':
                    geometry = feature['geometry']['coordinates']
                    for ring in geometry:
                        points = []
                        for coord in ring:
                            x_val, y_val = coord[0], coord[1]
                            x_prop = (x_val / 4096)
                            y_prop = (y_val / 4096)
                            lon_delta = lon2 - lon1
                            lat_delta = lat2 - lat1
                            lon_mapped = lon1 + (x_prop * lon_delta)
                            lat_mapped = lat1 + (y_prop * lat_delta)
                            lon_mapped, lat_mapped = transformer2.transform(
                                lon_mapped, lat_mapped)
                            point = rh.Point3d(
                                lon_mapped, lat_mapped, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        buildings.append(extrusion)
                elif geometry_type == 'MultiPolygon':
                    geometry = feature['geometry']['coordinates']
                    for polygon in geometry:
                        for ring in polygon:
                            points = []
                            for coord in ring:
                                x_val, y_val = coord[0], coord[1]
                                x_prop = (x_val / 4096)
                                y_prop = (y_val / 4096)
                                lon_delta = lon2 - lon1
                                lat_delta = lat2 - lat1
                                lon_mapped = lon1 + \
                                    (x_prop * lon_delta)
                                lat_mapped = lat1 + \
                                    (y_prop * lat_delta)
                                lon_mapped, lat_mapped = transformer2.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                            polyline = rh.Polyline(points)
                            curve = polyline.ToNurbsCurve()
                            orientation = curve.ClosedCurveOrientation()
                            if str(orientation) == 'CurveOrientation.Clockwise':
                                curve.Reverse()
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            buildings.append(extrusion)
        else:
            time.sleep(0)

    topo_url = metro_topo_url

    terrain_curves = []
    terrain_elevations = []
    counter = 0
    while True:
        topography_response = requests.get(
            topo_url, params=t_params)
        if topography_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    topography_data = json.loads(topography_response.text)
    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['altitude']
            geometry = feature["geometry"]
            for ring in geometry["paths"]:
                points = []
                points_e = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    point_e = rh.Point3d(
                        coord[0], coord[1], elevation)
                    points.append(point)
                    points_e.append(point_e)
                polyline = rh.Polyline(points)
                polyline_e = rh.Polyline(points_e)
                curve = polyline.ToNurbsCurve()
                curve_e = polyline_e.ToNurbsCurve()
                terrain_curves.append(curve)
                terrain_elevations.append(int(elevation))
                att = rh.ObjectAttributes()
                att.LayerIndex = contours_layer_EIndex
                att.SetUserString("Elevation", str(elevation))
                vic_e.Objects.AddCurve(
                    curve_e, att)
    else:
        time.sleep(0)

    mesh_geo_list = []
    curves_list_terrain = [
        {"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(terrain_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_list_terrain[0]["InnerTree"][key] = value

    elevations_list_terrain = [
        {"ParamName": "Elevations", "InnerTree": {}}]
    for i, elevation in enumerate(terrain_elevations):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": elevation
            }
        ]
        elevations_list_terrain[0]["InnerTree"][key] = value

    centre_list = []
    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    centre_list.append(centroid)

    centre_point_list = [{"ParamName": "Point", "InnerTree": {}}]
    for i, point in enumerate(centre_list):
        serialized_point = json.dumps(point, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Point",
                "data": serialized_point
            }
        ]
        centre_point_list[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_topography_decoded,
        "pointer": None,
        "values": curves_list_terrain + elevations_list_terrain + centre_point_list
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        innerTree = val['InnerTree']
        for key, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    mesh_geo = rh.CommonObject.Decode(data)
                    mesh_geo_list.append(mesh_geo)
                    att = rh.ObjectAttributes()
                    att.LayerIndex = topography_layerIndex
                    vic_e.Objects.AddMesh(mesh_geo, att)

    buildings_elevated = [
        {"ParamName": "Buildings", "InnerTree": {}}]

    for i, brep in enumerate(buildings):
        serialized_extrusion = json.dumps(
            brep, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Extrusion",
                "data": serialized_extrusion
            }
        ]
        buildings_elevated[0]["InnerTree"][key] = value

    mesh_terrain = [{"ParamName": "Mesh", "InnerTree": {}}]
    for i, mesh in enumerate(mesh_geo_list):
        serialized = json.dumps(mesh, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Mesh",
                "data": serialized
            }
        ]
        mesh_terrain[0]["InnerTree"][key] = value

    bound_curves = [{"ParamName": "Boundary", "InnerTree": {}}]
    for i, curve in enumerate(bound_curves_list):
        serialized = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized
            }
        ]
        bound_curves[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_buildings_elevated_decoded,
        "pointer": None,
        "values": buildings_elevated + mesh_terrain + bound_curves
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
    response_object = json.loads(res.content)['values']
    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:Elevated':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layer_EIndex
                        vic_e.Objects.AddBrep(geo, att)
        elif paramName == 'RH_OUT:UpBound':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = boundary_layerIndex
                        vic_e.Objects.AddCurve(geo, att)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:
        bound_curve.Translate(translation_vector)

    for obj in vic_e.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "vic_elevated.3dm"
    vic_e.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/carbon', methods=['GET', 'POST'])
def carbon():
    total_carbon = session.get('total_carbon')
    roof_carbon = session.get('roof_carbon')
    wall_carbon = session.get('wall_carbon')
    slab_carbon = session.get('slab_carbon')
    column_carbon = session.get('column_carbon')
    beam_carbon = session.get('beam_carbon')
    gwp = session.get('gwp')

    return render_template('carbon.html', total_carbon=total_carbon, roof_carbon=roof_carbon, wall_carbon=wall_carbon, slab_carbon=slab_carbon, column_carbon=column_carbon, beam_carbon=beam_carbon, gwp=gwp)

@application.route('/get_carbon', methods=['POST'])
def get_carbon():

    file = request.files['uploadCarbonFile']
    if file:
        file_path = 'tmp/files/' + file.filename
        file.save(file_path)
    else:
        return jsonify({'error': True})
    
    gridU = int(request.form['gridU'])
    gridV = int(request.form['gridV'])
    
    rhFile = rh.File3dm.Read(file_path)
    layers = rhFile.Layers

    shed = []
    office = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "INDUSTRIAL SHED":
            shed.append(obj)
        if layers[layer_index].Name == "INDUSTRIAL OFFICE":
            office.append(obj)

    shed_breps = [obj.Geometry for obj in shed]
    office_breps = [obj.Geometry for obj in office]

    serialized_shed = []
    for brep in shed_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_shed.append(serialized_brep)

    serialized_office = []
    for brep in office_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_office.append(serialized_brep)

    shed_list = [{"ParamName": "Shed", "InnerTree": {}}]
    for i, brep in enumerate(serialized_shed):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        shed_list[0]["InnerTree"][key] = value

    office_list = [{"ParamName": "Office", "InnerTree": {}}]
    for i, brep in enumerate(serialized_office):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        office_list[0]["InnerTree"][key] = value

    office_floors_list = []
    office_floors_list.append(2)

    send_office_floors = [{"ParamName": "Office Floors", "InnerTree": {}}]
    for i, num in enumerate(office_floors_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": num
            }
        ]
        send_office_floors[0]["InnerTree"][key] = value

    gridU_list = []
    gridU_list.append(gridU)

    send_gridU_list = [{"ParamName": "Grid U", "InnerTree": {}}]
    for i, num in enumerate(gridU_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": num
            }
        ]
        send_gridU_list[0]["InnerTree"][key] = value

    gridV_list = []
    gridV_list.append(gridV)

    send_gridV_list = [{"ParamName": "Grid V", "InnerTree": {}}]
    for i, num in enumerate(gridV_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": num
            }
        ]
        send_gridV_list[0]["InnerTree"][key] = value

    roof_c = int(request.form['roofChoice'])
    roof_c_list = []
    roof_c_list.append(roof_c)

    send_roof_c_list = [{"ParamName": "Roof Carbon", "InnerTree": {}}]
    for i, num in enumerate(roof_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_roof_c_list[0]["InnerTree"][key] = value

    slab_c = int(request.form['slabConcrete'])
    slab_c_list = []
    slab_c_list.append(slab_c)

    send_slab_c_list = [{"ParamName": "Slab Carbon", "InnerTree": {}}]
    for i, num in enumerate(slab_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_slab_c_list[0]["InnerTree"][key] = value

    wall_c = int(request.form['wallConcrete'])
    wall_c_list = []
    wall_c_list.append(wall_c)

    send_wall_c_list = [{"ParamName": "Wall Carbon", "InnerTree": {}}]
    for i, num in enumerate(wall_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_wall_c_list[0]["InnerTree"][key] = value

    column_c = int(request.form['columnChoice'])
    column_c_list = []
    column_c_list.append(column_c)

    send_column_c_list = [{"ParamName": "Column Carbon", "InnerTree": {}}]
    for i, num in enumerate(column_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_column_c_list[0]["InnerTree"][key] = value

    beam_c = int(request.form['beamChoice'])
    beam_c_list = []
    beam_c_list.append(beam_c)

    send_beam_c_list = [{"ParamName": "Beam Carbon", "InnerTree": {}}]
    for i, num in enumerate(beam_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_beam_c_list[0]["InnerTree"][key] = value

    gh_carbon = open(r"./carbon.ghx", mode="r",
                        encoding="utf-8-sig").read()
    gh_carbon_bytes = gh_carbon.encode("utf-8")
    gh_carbon_encoded = base64.b64encode(gh_carbon_bytes)
    gh_carbon_decoded = gh_carbon_encoded.decode("utf-8")

    geo_payload = {
        "algo": gh_carbon_decoded,
        "pointer": None,
        "values": shed_list + office_list + send_office_floors + send_gridV_list + send_gridU_list + send_roof_c_list + send_beam_c_list + send_slab_c_list + send_wall_c_list + send_column_c_list
    }

    res = requests.post(compute_url + "grasshopper", json=geo_payload, headers=headers)
    response_object = json.loads(res.content)['values']


    new_rhFile = rh.File3dm()
    new_rhFile.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    roof_layer = rh.Layer()
    roof_layer.Name = "Roof"
    roof_layerIndex = new_rhFile.Layers.Add(roof_layer)

    slab_layer = rh.Layer()
    slab_layer.Name = "Slab"
    slab_layerIndex = new_rhFile.Layers.Add(slab_layer)

    wall_layer = rh.Layer()
    wall_layer.Name = "Wall"
    wall_layerIndex = new_rhFile.Layers.Add(wall_layer)

    column_layer = rh.Layer()
    column_layer.Name = "Column"
    column_layerIndex = new_rhFile.Layers.Add(column_layer)

    beam_layer = rh.Layer()
    beam_layer.Name = "Beam"
    beam_layerIndex = new_rhFile.Layers.Add(beam_layer)

    for val in response_object:
        paramName = val['ParamName']
        if paramName == "RH_OUT:GFA":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        gfa = round(float(json.loads(innerVal['data'])), 2)
        if paramName == "RH_OUT:Roof":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        roof_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['roof_carbon'] = roof_carbon
        if paramName == "RH_OUT:Slabs":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        slab_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['slab_carbon'] = slab_carbon
        if paramName == "RH_OUT:Walls":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        wall_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['wall_carbon'] = wall_carbon
        if paramName == "RH_OUT:Columns":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        column_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['column_carbon'] = column_carbon
        if paramName == "RH_OUT:Beams":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        beam_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['beam_carbon'] = beam_carbon
        if paramName == "RH_OUT:TotalCarbon":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        total_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['total_carbon'] = total_carbon
        if paramName == 'RH_OUT:MeshRoof':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = roof_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshSlab':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = slab_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshWall':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = wall_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshColumn':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = column_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshBeam':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = beam_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
    
    gwp = total_carbon/gfa
    session['gwp'] = gwp

    filename = "carbon_output.3dm"
    new_rhFile.Write('./tmp/files/' + str(filename))
    new_rhFile.Write('./static/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/mergeRhino', methods=['POST'])
def merge_3dms():

    merged_model = rh.File3dm()
    merged_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    input_files = request.files.getlist('uploadedMergeFile')

    for file in input_files:
        file_path = 'tmp/files/merge/' + file.filename
        file.save(file_path)
        model = rh.File3dm.Read(file_path)
        for obj in model.Objects:
            layer_name = model.Layers[obj.Attributes.LayerIndex].FullPath
            layer_index = -1
            for i, layer in enumerate(merged_model.Layers):
                if layer.FullPath == layer_name:
                    layer_index = i
                    break
            if layer_index < 0:
                merged_model.Layers.Add(model.Layers[obj.Attributes.LayerIndex])
                layer_index = len(merged_model.Layers) - 1
            geometry = obj.Geometry.Duplicate()
            attributes = rh.ObjectAttributes()
            attributes.LayerIndex = layer_index

            user_strings = obj.Attributes.GetUserStrings()
            if user_strings:
                for key, value in user_strings:
                    attributes.SetUserString(key, value)

            merged_model.Objects.Add(geometry, attributes)

    filename = "merged.3dm"
    merged_model.Write('./tmp/files/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/environmental', methods=['GET', 'POST'])
def environmental():

    total_sunlight_hours = session.get('total_sunlight_hours', None)
    start_month = session.get('start_month')
    end_month = session.get('end_month')
    start_day = session.get('start_day')
    end_day = session.get('end_day')

    return render_template('environmental.html', total_sunlight_hours=total_sunlight_hours,
                           start_month=start_month, end_month=end_month,
                           start_day=start_day, end_day=end_day)

@application.route('/submit_environmental', methods=['POST'])
def submit_environmental():
    file = request.files['uploadFile']
    if file:
        file_path = 'tmp/files/' + file.filename
        file.save(file_path)
    else:
        return jsonify({'error': True})
    
    start_m = int(request.form['minMonth'])
    end_m = int(request.form['maxMonth'])
    start_d = int(request.form['minDay'])
    end_d = int(request.form['maxDay'])
    
    session['start_month'] = start_m
    session['end_month'] = end_m
    session['start_day'] = start_d
    session['end_day'] = end_d

    rhFile = rh.File3dm.Read(file_path)
    layers = rhFile.Layers
    context_list = []
    geometry_list = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Buildings":
            context_list.append(obj)
        if layers[layer_index].Name == "Geometry":
            geometry_list.append(obj)

    context_breps = [obj.Geometry for obj in context_list]

    geometry_breps = [obj.Geometry for obj in geometry_list]

    serialized_context = []
    for brep in context_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_context.append(serialized_brep)

    serialized_geometry = []
    for brep in geometry_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_geometry.append(serialized_brep)

    context_list_send = [{"ParamName": "context", "InnerTree": {}}]
    for i, brep_context in enumerate(serialized_context):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep_context
            }
        ]
        context_list_send[0]["InnerTree"][key] = value

    geometry_list_send = [{"ParamName": "geo", "InnerTree": {}}]
    for i, brep_geometry in enumerate(serialized_geometry):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep_geometry
            }
        ]
        geometry_list_send[0]["InnerTree"][key] = value

    start_m_list = []
    start_m_list.append(start_m)

    start_m_dict = [{"ParamName": "Start_M", "InnerTree": {}}]
    for i, s_month in enumerate(start_m_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": s_month
            }
        ]
        start_m_dict[0]["InnerTree"][key] = value

    start_d_list = []
    start_d_list.append(start_d)

    start_d_dict = [{"ParamName": "Start_D", "InnerTree": {}}]
    for i, s_day in enumerate(start_d_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": s_day
            }
        ]
        start_d_dict[0]["InnerTree"][key] = value

    start_h_list = []
    start_h_list.append(8)

    start_h_dict = [{"ParamName": "Start_H", "InnerTree": {}}]
    for i, s_hour in enumerate(start_h_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": s_hour
            }
        ]
        start_h_dict[0]["InnerTree"][key] = value

    end_m_list = []
    end_m_list.append(end_m)

    end_m_dict = [{"ParamName": "End_M", "InnerTree": {}}]
    for i, e_month in enumerate(end_m_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": e_month
            }
        ]
        end_m_dict[0]["InnerTree"][key] = value

    end_d_list = []
    end_d_list.append(end_d)

    end_d_dict = [{"ParamName": "End_D", "InnerTree": {}}]
    for i, e_day in enumerate(end_d_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": e_day
            }
        ]
        end_d_dict[0]["InnerTree"][key] = value

    end_h_list = []
    end_h_list.append(20)

    end_h_dict = [{"ParamName": "End_H", "InnerTree": {}}]
    for i, e_hour in enumerate(end_h_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Int32",
                "data": e_hour
            }
        ]
        end_h_dict[0]["InnerTree"][key] = value

    gh_sunlight_decoded = encode_ghx_file(r"./gh_scripts/sunlight.ghx")

    geo_payload = {
        "algo": gh_sunlight_decoded,
        "pointer": None,
        "values": context_list_send + geometry_list_send + start_h_dict + start_d_dict + start_m_dict + end_h_dict + end_d_dict + end_m_dict
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper",
                            json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    response_object = json.loads(res.content)['values']

    new_rhFile = rh.File3dm.Read(file_path)

    sunlight_layerIndex = create_layer(
        new_rhFile, "Sunlight", (129, 168, 0, 255))
    shadow_layerIndex = create_layer(
        new_rhFile, "Shadow", (129, 168, 0, 255))

    for val in response_object:
        paramName = val['ParamName']
        if paramName == 'RH_OUT:mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = sunlight_layerIndex
                        new_rhFile.Objects.AddMesh(geo, att)
        elif paramName == "RH_OUT:total_sunlight":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        total_sunlight_hours = round(
                            float(json.loads(innerVal['data'])), 2)
                        session['total_sunlight_hours'] = total_sunlight_hours
        elif paramName == 'RH_OUT:shadow_mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = shadow_layerIndex
                        new_rhFile.Objects.AddMesh(geo, att)

    layers = new_rhFile.Layers

    for layer in layers:
        if layer.Name == 'Geometry':
            layer.Visible = False

    filename = "environmental.3dm"
    new_rhFile.Write('./tmp/files/' + str(filename))
    new_rhFile.Write('./static/' + str(filename))

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

if __name__ == '__main__':
    application.run(host='0.0.0.0', port=5000, debug=True)
