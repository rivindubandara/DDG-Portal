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
from PIL import Image
from io import BytesIO
import os
import zipfile
from specklepy.api.client import SpeckleClient
from specklepy.api.client import get_account_from_token

Image.MAX_IMAGE_PIXELS = None

application = Flask(__name__, static_url_path='/static', static_folder='static')
application.secret_key = 'nettletontribe_secret_key'

mapbox_access_token = 'pk.eyJ1Ijoicml2aW5kdWIiLCJhIjoiY2xmYThkcXNjMHRkdDQzcGU4Mmh2a3Q3MSJ9.dXlhamKyYyGusL3PWqDD9Q'

compute_url = "http://13.54.229.195:80/"
# compute_url = "http://localhost:6500/"
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

def create_parameters_vic(geometry, geometry_type, xmin_LL, ymin_LL, xmax_LL, ymax_LL):
    params = {
        'where': '1=1',
        'geometry': f'{geometry}',
        'geometryType': f'{geometry_type}',
        'spatialRel': 'esriSpatialRelIntersects',
        'returnGeometry': 'true',
        'f': 'json',
        'outFields': '*',
        'inSR': '4326',
        'outSR': '32755'
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


def add_mesh_to_model(data, layerIndex, p_key, paramName, gh_algo, model):
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

    # sorted_names = []
    res = send_compute_post(geo_payload)
    response_object = json.loads(res.content)['values']

    colors = []
    surfaces = []
    string_vals = []

    for val in response_object:
        paramName = val['ParamName']
        innerTree = val.get('InnerTree', {})
        for _, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    if paramName == "RH_OUT:Colors":
                        colors.append(data)
                    elif paramName == "RH_OUT:Surface":
                        geo = rh.CommonObject.Decode(data)
                        surfaces.append(geo)
                    elif paramName == "RH_OUT:Values":
                        string_vals.append(data)

    for idx, (color, geo) in enumerate(zip(colors, surfaces)):
        r, g, b = map(int, color.split(','))
        att = rh.ObjectAttributes()
        a = 255
        att.ColorSource = rh.ObjectColorSource.ColorFromObject
        att.LayerIndex = layerIndex
        att.SetUserString(p_key, str(string_vals[idx]))
        att.ObjectColor = (r, g, b, int(a))
        model.Objects.AddBrep(geo, att)
    
    # if hasattr(res, 'content'):
    #     response_object = json.loads(res.content)['values']
    #     for val in response_object:
    #         if val['ParamName'] == gh_param:
    #             innerTree = val['InnerTree']
    #             for key, innerVals in innerTree.items():
    #                 for innerVal in innerVals:
    #                     if 'data' in innerVal:
    #                         data = json.loads(innerVal['data'])
    #                         sorted_names.append(data)

    #     i = 0
    #     for val in response_object:
    #         if val['ParamName'] == 'RH_OUT:Mesh':
    #             innerTree = val['InnerTree']
    #             for key, innerVals in innerTree.items():
    #                 for innerVal in innerVals:
    #                     if 'data' in innerVal:
    #                         data = json.loads(innerVal['data'])
    #                         geo = rh.CommonObject.Decode(data)
    #                         att = rh.ObjectAttributes()
    #                         att.LayerIndex = layerIndex
    #                         att.SetUserString(paramName, sorted_names[i])
    #                         model.Objects.AddMesh(geo, att)
    #                         i += 1

gh_interpolate_decoded = encode_ghx_file("./gh_scripts/interpolate.ghx")

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

transformer2 = Transformer.from_crs("EPSG:4326", "EPSG:32756", always_xy=True)
transformer = Transformer.from_crs("EPSG:3857", "EPSG:32756")

transformer2_vic = Transformer.from_crs("EPSG:4326", "EPSG:32755", always_xy=True)
transformer_vic = Transformer.from_crs("EPSG:3857", "EPSG:32755")

application.config['UPLOAD_FOLDER'] = '/upload'


@application.route('/', methods=['GET', 'POST'])
def index():
    return render_template('index.html')


@application.route('/submit/planning', methods=['POST'])
def get_planning():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    z_xmin_LL, z_xmax_LL, z_ymin_LL, z_ymax_LL = create_boundary(
        lat, lon, 10000)
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

    z_params = {
        'where': '1=1',
        'geometry': f'{z_xmin_LL}, {z_ymin_LL},{z_xmax_LL},{z_ymax_LL}',
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelIntersects',
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
    road_raw_layerIndex = create_layer(
        planning_model, "Roads (RAW)", (145, 145, 145, 255))
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
    raster_layerIndex = create_layer(
        planning_model, "Raster", (0, 204, 0, 255))

    gh_fsr_decoded = encode_ghx_file(r"./gh_scripts/fsr.ghx")
    gh_hob_decoded = encode_ghx_file(r"./gh_scripts/hob.ghx")
    gh_mls_decoded = encode_ghx_file(r"./gh_scripts/mls.ghx")
    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/zoning.ghx")
    gh_acid_decoded = encode_ghx_file(r"./gh_scripts/acid.ghx")
    gh_parks_decoded = encode_ghx_file(r"./gh_scripts/parks.ghx")
    gh_bushfire_decoded = encode_ghx_file(r"./gh_scripts/bushfire.ghx")
    gh_flood_decoded = encode_ghx_file(r"./gh_scripts/flood.ghx")
    gh_interpolate_decoded = encode_ghx_file(
        r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_raster_decoded = encode_ghx_file(r"./gh_scripts/image.ghx")

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

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            planning_model.Objects.AddCurve(bound_curve, att)

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

    # add_to_model(admin_data, admin_layerIndex,
    #              'suburbname', 'Suburb', planning_model)
    
    admin_curves = []
    admin_vals = []

    while True:
        if 'features' in admin_data:
            break
        else:
            time.sleep(0)
    for feature in admin_data["features"]:
                value = feature['attributes']['suburbname']
                admin_vals.append(value)
                geometry = feature["geometry"]
                for ring in geometry["rings"]:
                    points = []
                    for coord in ring:
                        point = rh.Point3d(coord[0], coord[1], 0)
                        points.append(point)
                    polyline = rh.Polyline(points)
                    curve = polyline.ToNurbsCurve()
                    admin_curves.append(curve)

    curves_to_send = [{"ParamName": "Curves", "InnerTree": {}}]

    for i, curve in enumerate(admin_curves):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        curves_to_send[0]["InnerTree"][key] = value

    val_to_send = [{"ParamName": "Admin", "InnerTree": {}}]

    for i, val in enumerate(admin_vals):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": val
            }
        ]
        val_to_send[0]["InnerTree"][key] = value


    gh_admin_decoded = encode_ghx_file('./gh_scripts/admin.ghx')

    geo_payload = {
        "algo": gh_admin_decoded,
        "pointer": None,
        "values": val_to_send + curves_to_send
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

    colors = []
    surfaces = []
    string_vals = []

    for val in response_object:
        paramName = val['ParamName']
        innerTree = val.get('InnerTree', {})

        for _, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    if paramName == "RH_OUT:Colors":
                        colors.append(data)
                    elif paramName == "RH_OUT:Surface":
                        geo = rh.CommonObject.Decode(data)
                        surfaces.append(geo)
                    elif paramName == "RH_OUT:Values":
                        string_vals.append(data)

    for idx, (color, geo) in enumerate(zip(colors, surfaces)):
        r, g, b = map(int, color.split(','))
        a = 255
        att = rh.ObjectAttributes()
        att.ColorSource = rh.ObjectColorSource.ColorFromObject
        att.LayerIndex = admin_layerIndex
        att.SetUserString("Suburb", str(string_vals[idx]))
        att.ObjectColor = (r, g, b, int(a))
        planning_model.Objects.AddBrep(geo, att)
    
    zoning_curves = []
    zoning_names = []
    
    counter = 0
    while True:
        zoning_response = requests.get(zoning_url, params=z_params)
        if zoning_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)

    zoning_data = json.loads(zoning_response.text)
    if "features" in zoning_data:
        for feature in zoning_data["features"]:
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

    colors = []
    surfaces = []
    string_vals = []

    for val in response_object:
        paramName = val['ParamName']
        innerTree = val.get('InnerTree', {})
        for _, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    if paramName == "RH_OUT:Colors":
                        colors.append(data)
                    elif paramName == "RH_OUT:Surface":
                        geo = rh.CommonObject.Decode(data)
                        surfaces.append(geo)
                    elif paramName == "RH_OUT:Values":
                        string_vals.append(data)

    for idx, (color, geo) in enumerate(zip(colors, surfaces)):
        r, g, b, a = map(int, color.split(','))
        att = rh.ObjectAttributes()
        att.ColorSource = rh.ObjectColorSource.ColorFromObject
        att.LayerIndex = zoning_layerIndex
        att.SetUserString("Zoning Code", str(string_vals[idx]))
        att.ObjectColor = (r, g, b, a)
        planning_model.Objects.AddBrep(geo, att)

    # for val in response_object:
    #     paramName = val['ParamName']
    #     if paramName == 'RH_OUT:Zone':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     zoning_names_sorted.append(data)
    # i = 0
    # for val in response_object:
    #     paramName = val['ParamName']
    #     if paramName == 'RH_OUT:Mesh':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     geo = rh.CommonObject.Decode(data)
    #                     att = rh.ObjectAttributes()
    #                     att.LayerIndex = zoning_layerIndex
    #                     att.SetUserString(
    #                         "Zoning Code", zoning_names_sorted[i])
    #                     planning_model.Objects.AddMesh(
    #                         geo, att)
    #                     i += 1
       
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

    colors = []
    surfaces = []
    string_vals = []

    for val in response_object:
        paramName = val['ParamName']
        innerTree = val.get('InnerTree', {})
        for _, innerVals in innerTree.items():
            for innerVal in innerVals:
                if 'data' in innerVal:
                    data = json.loads(innerVal['data'])
                    if paramName == "RH_OUT:Colors":
                        colors.append(data)
                    elif paramName == "RH_OUT:Surface":
                        geo = rh.CommonObject.Decode(data)
                        surfaces.append(geo)
                    elif paramName == "RH_OUT:Values":
                        string_vals.append(data)

    for idx, (color, geo) in enumerate(zip(colors, surfaces)):
        r, g, b = map(int, color.split(','))
        att = rh.ObjectAttributes()
        a = 255
        att.ColorSource = rh.ObjectColorSource.ColorFromObject
        att.LayerIndex = hob_layerIndex
        att.SetUserString("HOB", str(string_vals[idx]))
        att.ObjectColor = (r, g, b, int(a))
        planning_model.Objects.AddBrep(geo, att)

    # hob_numbers_sorted = []
    # counter = 0
    # while True:
    #     res = requests.post(compute_url + "grasshopper",
    #                         json=geo_payload, headers=headers)
    #     if res.status_code == 200:
    #         break
    #     else:
    #         counter += 1
    #         if counter > 1:
    #             break
    #         time.sleep(0)
    # response_object = json.loads(res.content)['values']
    # for val in response_object:
    #     paramName = val['ParamName']
    #     if paramName == 'RH_OUT:HOBnum':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     hob_numbers_sorted.append(data)

    # i = 0
    # for val in response_object:
    #     paramName = val['ParamName']
    #     if paramName == 'RH_OUT:Mesh':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     geo = rh.CommonObject.Decode(data)
    #                     att = rh.ObjectAttributes()
    #                     att.LayerIndex = hob_layerIndex
    #                     att.SetUserString("HOB", str(hob_numbers_sorted[i]))
    #                     planning_model.Objects.AddMesh(geo, att)
    #                     i += 1

    add_mesh_to_model(lotsize_data, lotsize_layerIndex, 'LOT_SIZE',
                      'MLS', gh_mls_decoded, planning_model)

    add_mesh_to_model(fsr_data, fsr_layerIndex, 'FSR', 'FSR',
                      gh_fsr_decoded, planning_model)

    add_to_model(lots_data, lots_layerIndex,
                 "plannumber", "Lot Number", planning_model)

    add_to_model(plan_extent_data, plan_extent_layerIndex,
                 "planoid", "Plan Extent Number", planning_model)

    add_to_model(acid_data, acid_layerIndex,
                 "LAY_CLASS", "Acid Class", planning_model)

    add_mesh_to_model(bushfire_data, bushfire_layerIndex,
                 "d_Category", "Bushfire", gh_bushfire_decoded, planning_model)

    add_to_model(flood_data, flood_layerIndex,
                 "LAY_CLASS", "Flood", planning_model)

    add_to_model(heritage_data, heritage_layerIndex,
                 "H_NAME", "Heritage Name", planning_model)
    
    add_to_model(parks_data, parks_layerIndex,
                 "NAME", "Park Name", planning_model)

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

    road_curves = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                road_class = feature['properties']['class']
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
                att = rh.ObjectAttributes()
                att.LayerIndex = road_raw_layerIndex
                att.SetUserString("Road Class", str(road_class))
                planning_model.Objects.AddCurve(
                curve, att)
                road_curves.append(curve)

            elif geometry_type == 'MultiLineString':
                geometry = feature['geometry']['coordinates']
                road_class = feature['properties']['class']
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
                    att = rh.ObjectAttributes()
                    att.LayerIndex = road_raw_layerIndex
                    att.SetUserString("Road Class", str(road_class))
                    planning_model.Objects.AddCurve(
                    curve, att)
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
    
    ras_xmin_LL, ras_xmax_LL, ras_ymin_LL, ras_ymax_LL = create_boundary(lat, lon, 1000)

    ras_tiles = list(mercantile.tiles(ras_xmin_LL, ras_ymin_LL, ras_xmax_LL, ras_ymax_LL, zooms=16))

    for tile in ras_tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.satellite/{zoom}/{tile.x}/{tile.y}@2x.png256?access_token={mapbox_access_token}"
        response = requests.get(mb_url)

        if response.status_code == 200:
            image_data = BytesIO(response.content)
            image = Image.open(image_data)
            file_name = "ras.png"
            image.save('./tmp/' + file_name)

    rastile = ras_tiles[0] 

    bbox = mercantile.bounds(rastile)
    lon1, lat1, lon2, lat2 = bbox
    t_lon1, t_lat1 = transformer2.transform(lon1, lat1)
    t_lon2, t_lat2 = transformer2.transform(lon2, lat2)

    raster_points = [
        rh.Point3d(t_lon1, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat1, 0)
    ]

    points_list = rh.Point3dList(raster_points)
    raster_curve = rh.PolylineCurve(points_list)
    raster_curve = raster_curve.ToNurbsCurve()

    with open('./tmp/' + file_name, 'rb') as img_file:
        img_bytes = img_file.read()

    b64_string = base64.b64encode(img_bytes).decode('utf-8')

    string_encoded = b64_string
    send_string = [{"ParamName": "BaseString", "InnerTree": {}}]

    serialized_string = json.dumps(string_encoded, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "System.String",
            "data": serialized_string
        }
    ]
    send_string[0]["InnerTree"][key] = value

    curve_payload = [{"ParamName": "Curve", "InnerTree": {}}]
    serialized_curve = json.dumps(raster_curve, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "Rhino.Geometry.Curve",
            "data": serialized_curve
        }
    ]
    curve_payload[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_raster_decoded,
        "pointer": None,
        "values": send_string + curve_payload
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
                    att.LayerIndex = raster_layerIndex
                    planning_model.Objects.AddMesh(geo, att)

    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:  
        bound_curve.Translate(translation_vector)

    for obj in planning_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  
            obj.Geometry.Translate(translation_vector)

    filename = "planning.3dm"
    planning_model.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/submit/geometry', methods=['POST'])
def get_geometry():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'
    hob_url = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning/EPI_Primary_Planning_Layers/MapServer/5/query"

    # gh_procedural_decoded = encode_ghx_file(r"./gh_scripts/PropertyOffsets.ghx")

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
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
    buildingfootprint_LayerIndex = create_layer(geometry_model, "Building Footprint",(191, 191, 191, 255) )
    procedural_layerIndex = create_layer(geometry_model, "Geometry", (0, 204, 0, 255))
    proceduralbuildings_layerIndex = create_layer(geometry_model, "Culled Geometry", (99, 99, 99, 255))

    params_dict = {
        topo_url: topo_params
    }

    urls = [
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

    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            geometry_model.Objects.AddCurve(bound_curve, att)
    
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
                        att_bf = rh.ObjectAttributes()
                        att_bf.LayerIndex = buildingfootprint_LayerIndex
                        geometry_model.Objects.AddCurve(curve, att_bf)
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        geometry_model.Objects.AddExtrusion(
                            extrusion, att)
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
                            att_bf = rh.ObjectAttributes()
                            att_bf.LayerIndex = buildingfootprint_LayerIndex
                            geometry_model.Objects.AddCurve(curve, att_bf)
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            geometry_model.Objects.AddExtrusion(
                                extrusion, att)
                            buildings.append(extrusion)
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

    # hob_data = get_data(hob_url, boundary_params)
    # if "features" in hob_data:
    #     for feature in hob_data["features"]:
    #         hob_num = feature['attributes']['MAX_B_H']
    #         if hob_num is None:
    #             hob_num = 3
    # else:
    #     time.sleep(0)

    # hob_list = [{"ParamName": "HOB", "InnerTree": {}}]
    # hobs_list = []
    # hobs_list.append(hob_num)

    # bound_list = [{"ParamName": "Boundary", "InnerTree": {}}]
    # bounds_list = []
    # bounds_list.append(bound_curve)

    # roads = []
    # for tile in tiles:
    #     mb_data = concurrent_fetching(zoom, tile)
    #     tiles1 = mapbox_vector_tile.decode(mb_data)

    #     if 'road' not in tiles1:
    #         continue

    #     road_layer = tiles1['road']

    #     tile1 = mercantile.Tile(tile.x, tile.y, 16)
    #     bbox = mercantile.bounds(tile1)
    #     lon1, lat1, lon2, lat2 = bbox

    #     for feature in road_layer['features']:
    #         geometry_type = feature['geometry']['type']
    #         if geometry_type == 'LineString':
    #             geometry = feature['geometry']['coordinates']
    #             road_class = feature['properties']['class']
    #             points = []
    #             for ring in geometry:
    #                 x_val, y_val = ring[0], ring[1]
    #                 x_prop = (x_val / 4096)
    #                 y_prop = (y_val / 4096)
    #                 lon_delta = lon2 - lon1
    #                 lat_delta = lat2 - lat1
    #                 lon_mapped = lon1 + (x_prop * lon_delta)
    #                 lat_mapped = lat1 + (y_prop * lat_delta)
    #                 lon_mapped, lat_mapped = transformer2.transform(
    #                     lon_mapped, lat_mapped)
    #                 point = rh.Point3d(lon_mapped, lat_mapped, 0)
    #                 points.append(point)

    #             polyline = rh.Polyline(points)
    #             curve = polyline.ToNurbsCurve()
    #             roads.append(curve)

    #         elif geometry_type == 'MultiLineString':
    #             geometry = feature['geometry']['coordinates']
    #             road_class = feature['properties']['class']
    #             for line_string in geometry:
    #                 points = []
    #                 for ring in line_string:
    #                     x_val, y_val = ring[0], ring[1]
    #                     x_prop = (x_val / 4096)
    #                     y_prop = (y_val / 4096)
    #                     lon_delta = lon2 - lon1
    #                     lat_delta = lat2 - lat1
    #                     lon_mapped = lon1 + (x_prop * lon_delta)
    #                     lat_mapped = lat1 + (y_prop * lat_delta)
    #                     lon_mapped, lat_mapped = transformer2.transform(
    #                         lon_mapped, lat_mapped)
    #                     point = rh.Point3d(
    #                         lon_mapped, lat_mapped, 0)
    #                     points.append(point)
    #                 polyline = rh.Polyline(points)
    #                 curve = polyline.ToNurbsCurve()
    #                 roads.append(curve)

    # road_list = [{"ParamName": "Roads", "InnerTree": {}}]

    # building_list = [{"ParamName": "Buildings", "InnerTree": {}}]

    # for i, num in enumerate(hobs_list):
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "System.Float",
    #             "data": num
    #         }
    #     ]
    #     hob_list[0]["InnerTree"][key] = value

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

    # for i, curve in enumerate(roads):
    #     serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "Rhino.Geometry.Curve",
    #             "data": serialized_curve
    #         }
    #     ]
    #     road_list[0]["InnerTree"][key] = value
    
    # for i, brep in enumerate(buildings):
    #     serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "Rhino.Geometry.Brep",
    #             "data": serialized_brep
    #         }
    #     ]
    #     building_list[0]["InnerTree"][key] = value

    # geo_payload = {
    #     "algo": gh_procedural_decoded,
    #     "pointer": None,
    #     "values": bound_list + hob_list + road_list + building_list
    # }

    # counter = 0
    # while True:
    #     res = requests.post(compute_url + "grasshopper",
    #                         json=geo_payload, headers=headers)
    #     if res.status_code == 200:
    #         break
    #     else:
    #         counter += 1
    #         if counter >= 3:
    #             return jsonify({'error': True})
    #         time.sleep(0)
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
    #                     geometry_model.Objects.AddBrep(geo, att)
    #     if paramName == 'RH_OUT:Existing':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     geo = rh.CommonObject.Decode(data)
    #                     att = rh.ObjectAttributes()
    #                     att.LayerIndex = proceduralbuildings_layerIndex
    #                     geometry_model.Objects.AddBrep(geo, att)

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
    geometry_model.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/submit/elevated', methods=['POST'])
def get_elevated():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
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
    mapboxContours_LayerIndex = create_layer(elevated_model, "Mapbox Contours Elevated", (191,191,191,255))

    gh_topography_decoded = encode_ghx_file(
        r"./gh_scripts/topography.ghx")
    gh_buildings_elevated_decoded = encode_ghx_file(
        r"./gh_scripts/elevate_buildings.ghx")
    gh_mapboxContours_decoded = encode_ghx_file(r"./gh_scripts/mapboxContours.ghx")

    params_dict = {
        topo_url: topo_params
    }

    urls = [
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

    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerEIndex
            att.SetUserString("Address", str(address))
            elevated_model.Objects.AddCurve(bound_curve, att)

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

    # mbc_xmin_LL, mbc_xmax_LL, mbc_ymin_LL, mbc_ymax_LL = create_boundary(lat, lon, 15000)
    # mbc_tiles = list(mercantile.tiles(mbc_xmin_LL, mbc_ymin_LL, mbc_xmax_LL, mbc_ymax_LL, zooms=14))

    # tilesX_list = []
    # tilesY_list = []
    # for tile in mbc_tiles:
    #     tilesX_list.append(tile.x)
    #     tilesY_list.append(tile.y)

    # tilesX_list = list(set(tilesX_list))
    # tilesY_list = list(set(tilesY_list))

    # tileX_send = [{"ParamName": "TileX", "InnerTree": {}}]
    # for i, val in enumerate(tilesX_list):
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "System.Int32",
    #             "data": val
    #         }
    #     ]
    #     tileX_send[0]["InnerTree"][key] = value

    # tileY_send = [{"ParamName": "TileY", "InnerTree": {}}]
    # for i, val in enumerate(tilesY_list):
    #     key = f"{{{i};0}}"
    #     value = [
    #         {
    #             "type": "System.Int32",
    #             "data": val
    #         }
    #     ]
    #     tileY_send[0]["InnerTree"][key] = value

    # geo_payload = {
    #     "algo": gh_mapboxContours_decoded,
    #     "pointer": None,
    #     "values":  tileX_send + tileY_send
    # }

    # res = requests.post(compute_url + "grasshopper",
    #                     json=geo_payload, headers=headers)
    # counter = 0
    # while True:
    #     if res.status_code == 200:
    #         break
    #     else:
    #         counter += 1
    #         if counter >= 3:
    #             None
    # response_object = json.loads(res.content)['values']
    # for val in response_object:
    #     paramName = val['ParamName']
    #     if paramName == 'RH_OUT:Contours':
    #         innerTree = val['InnerTree']
    #         for key, innerVals in innerTree.items():
    #             for innerVal in innerVals:
    #                 if 'data' in innerVal:
    #                     data = json.loads(innerVal['data'])
    #                     geo = rh.CommonObject.Decode(data)
    #                     att = rh.ObjectAttributes()
    #                     att.LayerIndex = mapboxContours_LayerIndex
    #                     elevated_model.Objects.AddCurve(geo, att)

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
    elevated_model.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/submit/lite', methods=['POST'])
def lite():
    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'
    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})
        
    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
            lat, lon, 30000)
    topo_params = create_parameters(
            '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)
    params = create_parameters('', 'esriGeometryEnvelope',
                                xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    lite_model = rh.File3dm()
    lite_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(
        lite_model, "Boundary", (237, 0, 194, 255))
    lots_layerIndex = create_layer(
        lite_model, "Lots", (255, 106, 0, 255))
    road_layerIndex = create_layer(
        lite_model, "Roads", (145, 145, 145, 255))
    building_layerIndex = create_layer(
        lite_model, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(
        lite_model, "Contours", (191, 191, 191, 255))
    topography_layerIndex = create_layer(
        lite_model, "Topography", (191, 191, 191, 255))
    gh_topography_decoded = encode_ghx_file(
        r"./gh_scripts/topography.ghx")

    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")

    lots_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'

    params_dict = {
        lots_url: params,
        topo_url: topo_params
    }
    urls = [
        lots_url,
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
                if url == lots_url:
                    data_dict['lots_data'] = data
                elif url == topo_url:
                    data_dict['topography_data'] = data

    lots_data = data_dict.get('lots_data')
    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            lite_model.Objects.AddCurve(bound_curve, att)

    add_to_model(lots_data, lots_layerIndex,"plannumber", "Lot Number", lite_model)

    road_curves = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                road_class = feature['properties']['class']
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
                road_class = feature['properties']['class']
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
                        lite_model.Objects.AddCurve(geo, att)

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
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        lite_model.Objects.AddExtrusion(
                            extrusion, att)
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
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            lite_model.Objects.AddExtrusion(
                                extrusion, att)
                            buildings.append(extrusion)
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
                lite_model.Objects.AddCurve(curve, att)
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
                    lite_model.Objects.AddMesh(mesh_geo, att)


    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                        centroid.Y, -centroid.Z)

    if bound_curve is not None:  
        bound_curve.Translate(translation_vector)

    for obj in lite_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  
            obj.Geometry.Translate(translation_vector)

    filename = "lite.3dm"
    lite_model.Write('./tmp/files/' + str(filename), 7)

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
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
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
    raster_layerIndex = create_layer(qld, "Raster", (255, 106, 0, 255))

    gh_admin_decoded = encode_ghx_file(r"./gh_scripts/admin.ghx")
    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/vic_qld_zoning.ghx")
    gh_interpolate_decoded = encode_ghx_file(r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_bushfire_decoded = encode_ghx_file(r"./gh_scripts/bushfire.ghx")
    gh_raster_decoded = encode_ghx_file(r"./gh_scripts/image.ghx")

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
    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
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

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            qld.Objects.AddCurve(bound_curve, att)

    counter = 0
    while True:
        native_response = requests.post(native_url, json=native_post)
        if native_response.status_code == 200:
            break
        else:
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
            time.sleep(0)
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
            time.sleep(0)
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
                time.sleep(0)
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

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
            time.sleep(0)
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

    iso_url_w = f'https://api.mapbox.com/isochrone/v1/{profile1}/{longitude_iso},{latitude_iso}?contours_minutes=5&polygons=true&access_token={mapbox_access_token}'

    iso_url_c = f'https://api.mapbox.com/isochrone/v1/{profile2}/{longitude_iso},{latitude_iso}?contours_minutes=10&polygons=true&access_token={mapbox_access_token}'

    iso_url_d = f'https://api.mapbox.com/isochrone/v1/{profile3}/{longitude_iso},{latitude_iso}?contours_minutes=15&polygons=true&access_token={mapbox_access_token}'

    iso_url_w_10 = f'https://api.mapbox.com/isochrone/v1/{profile1}/{longitude_iso},{latitude_iso}?contours_minutes=10&polygons=true&access_token={mapbox_access_token}'

    iso_url_w_15 = f'https://api.mapbox.com/isochrone/v1/{profile1}/{longitude_iso},{latitude_iso}?contours_minutes=15&polygons=true&access_token={mapbox_access_token}'

    iso_url_c_15 = f'https://api.mapbox.com/isochrone/v1/{profile2}/{longitude_iso},{latitude_iso}?contours_minutes=15&polygons=true&access_token={mapbox_access_token}'

    iso_url_c_5 = f'https://api.mapbox.com/isochrone/v1/{profile2}/{longitude_iso},{latitude_iso}?contours_minutes=5&polygons=true&access_token={mapbox_access_token}'

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

    counter = 0
    while True:
        iso_response_c_5 = requests.get(iso_url_c_5)
        if iso_response_c_5.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    cycling_data_5 = json.loads(iso_response_c_5.content.decode())

    counter = 0
    while True:
        iso_response_c_15 = requests.get(iso_url_c_15)
        if iso_response_c_15.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    cycling_data_15 = json.loads(iso_response_c_15.content.decode())

    counter = 0
    while True:
        iso_response_w_10 = requests.get(iso_url_w_10)
        if iso_response_w_10.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    walking_data_10 = json.loads(iso_response_w_10.content.decode())

    counter = 0
    while True:
        iso_response_w_15 = requests.get(iso_url_w_15)
        if iso_response_w_15.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    walking_data_15 = json.loads(iso_response_w_15.content.decode())

    add_curves_to_model(walking_data, transformer2,
                        walking_layerIndex, qld)
    add_curves_to_model(cycling_data, transformer2,
                        cycling_layerIndex, qld)
    add_curves_to_model(driving_data, transformer2,
                        driving_layerIndex, qld)
    add_curves_to_model(walking_data_10, transformer2,
                        walking_layerIndex, qld)
    add_curves_to_model(walking_data_15, transformer2,
                        walking_layerIndex, qld)
    add_curves_to_model(cycling_data_5, transformer2,
                        cycling_layerIndex, qld)
    add_curves_to_model(cycling_data_15, transformer2,
                        cycling_layerIndex, qld)

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
            time.sleep(0)
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
            time.sleep(0)
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

    ras_xmin_LL, ras_xmax_LL, ras_ymin_LL, ras_ymax_LL = create_boundary(lat, lon, 1000)

    ras_tiles = list(mercantile.tiles(ras_xmin_LL, ras_ymin_LL, ras_xmax_LL, ras_ymax_LL, zooms=16))

    for tile in ras_tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.satellite/{zoom}/{tile.x}/{tile.y}@2x.png256?access_token={mapbox_access_token}"
        response = requests.get(mb_url)

        if response.status_code == 200:
            image_data = BytesIO(response.content)
            image = Image.open(image_data)
            file_name = "ras.png"
            image.save('./tmp/' + file_name)

    rastile = ras_tiles[0] 

    bbox = mercantile.bounds(rastile)
    lon1, lat1, lon2, lat2 = bbox
    t_lon1, t_lat1 = transformer2.transform(lon1, lat1)
    t_lon2, t_lat2 = transformer2.transform(lon2, lat2)

    raster_points = [
        rh.Point3d(t_lon1, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat1, 0)
    ]

    points_list = rh.Point3dList(raster_points)
    raster_curve = rh.PolylineCurve(points_list)
    raster_curve = raster_curve.ToNurbsCurve()

    with open('./tmp/' + file_name, 'rb') as img_file:
        img_bytes = img_file.read()

    # Encode bytes to base64 string
    b64_string = base64.b64encode(img_bytes).decode('utf-8')

    string_encoded = b64_string
    send_string = [{"ParamName": "BaseString", "InnerTree": {}}]

    serialized_string = json.dumps(string_encoded, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "System.String",
            "data": serialized_string
        }
    ]
    send_string[0]["InnerTree"][key] = value

    curve_payload = [{"ParamName": "Curve", "InnerTree": {}}]
    serialized_curve = json.dumps(raster_curve, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "Rhino.Geometry.Curve",
            "data": serialized_curve
        }
    ]
    curve_payload[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_raster_decoded,
        "pointer": None,
        "values": send_string + curve_payload
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
                    att.LayerIndex = raster_layerIndex
                    qld.Objects.AddMesh(geo, att)

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
    qld.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/qld_geometry', methods=['POST'])
def get_qld_geometry():

    boundary_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/PlanningCadastre/LandParcelPropertyFramework/MapServer/8/query'
    topo_url = "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Elevation/ContoursCache/MapServer/0/query"

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    qld_g = rh.File3dm()
    qld_g.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(qld_g, "Boundary", (237, 0, 194, 255))
    building_layerIndex = create_layer(qld_g, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(qld_g, "Contours", (191, 191, 191, 255))
    geometry_layerIndex = create_layer(qld_g, "Geometry", (191, 191, 191, 255))
    buildingfootprint_LayerIndex = create_layer(qld_g, "Building Footprint", (191, 191, 191, 255))

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
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

    topography_data = data_dict.get('topography_data')

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            qld_g.Objects.AddCurve(bound_curve, att)

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
                        att_bf = rh.ObjectAttributes()
                        att_bf.LayerIndex = buildingfootprint_LayerIndex
                        qld_g.Objects.AddCurve(curve, att_bf)
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
                            att_bf = rh.ObjectAttributes()
                            att_bf.LayerIndex = buildingfootprint_LayerIndex
                            qld_g.Objects.AddCurve(curve, att_bf)
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
    qld_g.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/qld_elevated', methods=['POST'])
def get_qld_elevated():

    boundary_url = 'https://spatial-gis.information.qld.gov.au/arcgis/rest/services/PlanningCadastre/LandParcelPropertyFramework/MapServer/8/query'
    topo_url = "https://spatial-gis.information.qld.gov.au/arcgis/rest/services/Elevation/ContoursCache/MapServer/0/query"

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
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

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
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

    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerEIndex
            att.SetUserString("Address", str(address))
            qld_e.Objects.AddCurve(bound_curve, att)

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
    qld_e.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/vic_planning', methods=['POST'])
def get_vic_planning():

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
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
    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/vic_qld_zoning.ghx")
    gh_interpolate_decoded = encode_ghx_file(r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_lots_decoded = encode_ghx_file(r"./gh_scripts/vic_lots.ghx")
    gh_raster_decoded = encode_ghx_file(r"./gh_scripts/image.ghx")

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
    raster_layerIndex = create_layer(vic, "Raster", (153, 153, 153, 255))
    
    l_xmin_LL, l_xmax_LL, l_ymin_LL, l_ymax_LL = create_boundary(lat, lon, 10000)
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

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    l_xmin_LL, l_xmax_LL, l_ymin_LL, l_ymax_LL = create_boundary(
        lat, lon, 10000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 200000)
    n_xmin_LL, n_xmax_LL, n_ymin_LL, n_ymax_LL = create_boundary(
        lat, lon, 800000)

    boundary_params = create_parameters_vic(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters_vic(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            vic.Objects.AddCurve(bound_curve, att)

    counter = 0
    while True:
        native_response = requests.post(native_url, json=native_post)
        if native_response.status_code == 200:
            break
        else:
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
                native_x, native_y = transformer2_vic.transform(
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
            time.sleep(0)
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
            time.sleep(0)
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
            time.sleep(0)
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
            time.sleep(0)
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
            time.sleep(0)
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
            time.sleep(0)
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
                time.sleep(0)
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

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
                    lon_mapped, lat_mapped = transformer2_vic.transform(
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
                        lon_mapped, lat_mapped = transformer2_vic.transform(
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
            time.sleep(0)
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
                        vic.Objects.AddCurve(geo, att)

    add_curves_to_model(walking_data, transformer2_vic, walking_layerIndex, vic)
    add_curves_to_model(cycling_data, transformer2_vic, cycling_layerIndex, vic)
    add_curves_to_model(driving_data, transformer2_vic, driving_layerIndex, vic)

    ras_xmin_LL, ras_xmax_LL, ras_ymin_LL, ras_ymax_LL = create_boundary(lat, lon, 1000)

    ras_tiles = list(mercantile.tiles(ras_xmin_LL, ras_ymin_LL, ras_xmax_LL, ras_ymax_LL, zooms=16))

    for tile in ras_tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.satellite/{zoom}/{tile.x}/{tile.y}@2x.png256?access_token={mapbox_access_token}"
        response = requests.get(mb_url)

        if response.status_code == 200:
            image_data = BytesIO(response.content)
            image = Image.open(image_data)
            file_name = "ras.png"
            image.save('./tmp/' + file_name)

    rastile = ras_tiles[0] 

    bbox = mercantile.bounds(rastile)
    lon1, lat1, lon2, lat2 = bbox
    t_lon1, t_lat1 = transformer2_vic.transform(lon1, lat1)
    t_lon2, t_lat2 = transformer2_vic.transform(lon2, lat2)

    raster_points = [
        rh.Point3d(t_lon1, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat1, 0)
    ]

    points_list = rh.Point3dList(raster_points)
    raster_curve = rh.PolylineCurve(points_list)
    raster_curve = raster_curve.ToNurbsCurve()

    with open('./tmp/' + file_name, 'rb') as img_file:
        img_bytes = img_file.read()

    # Encode bytes to base64 string
    b64_string = base64.b64encode(img_bytes).decode('utf-8')

    string_encoded = b64_string
    send_string = [{"ParamName": "BaseString", "InnerTree": {}}]

    serialized_string = json.dumps(string_encoded, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "System.String",
            "data": serialized_string
        }
    ]
    send_string[0]["InnerTree"][key] = value

    curve_payload = [{"ParamName": "Curve", "InnerTree": {}}]
    serialized_curve = json.dumps(raster_curve, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "Rhino.Geometry.Curve",
            "data": serialized_curve
        }
    ]
    curve_payload[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_raster_decoded,
        "pointer": None,
        "values": send_string + curve_payload
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
                    att.LayerIndex = raster_layerIndex
                    vic.Objects.AddMesh(geo, att)

    cen_x, cen_y = transformer2_vic.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in vic.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "vic_planning.3dm"
    vic.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/vic_geometry', methods=['POST'])
def get_vic_geometry():

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
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
    buildingfootprint_LayerIndex = create_layer(vic_g, "Building Footprint", (191, 191, 191, 255))

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 400000)

    boundary_params = create_parameters_vic(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters_vic(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_params = create_parameters_vic(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            vic_g.Objects.AddCurve(bound_curve, att)

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
                time.sleep(0)
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
                        lon_mapped, lat_mapped = transformer2_vic.transform(
                            lon_mapped, lat_mapped)
                        point = rh.Point3d(
                            lon_mapped, lat_mapped, 0)
                        points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                orientation = curve.ClosedCurveOrientation()
                if str(orientation) == 'CurveOrientation.Clockwise':
                    curve.Reverse()
                att_bf = rh.ObjectAttributes()
                att_bf.LayerIndex = buildingfootprint_LayerIndex
                vic_g.Objects.AddCurve(curve, att_bf)
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
                                lon_mapped, lat_mapped = transformer2_vic.transform(
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
                                lon_mapped, lat_mapped = transformer2_vic.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        att_bf = rh.ObjectAttributes()
                        att_bf.LayerIndex = buildingfootprint_LayerIndex
                        vic_g.Objects.AddCurve(curve, att_bf)
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
    topo_url = regional_topo_url

    counter = 0
    while True:
        topography_response = requests.get(topo_url, params=t_params)
        if topography_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
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

    cen_x, cen_y = transformer2_vic.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:  # Check if bound_curve is not None
        bound_curve.Translate(translation_vector)

    for obj in vic_g.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  # Check if obj.Geometry is not None
            obj.Geometry.Translate(translation_vector)

    filename = "vic_geometry.3dm"
    vic_g.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/vic_elevated', methods=['POST'])
def get_vic_elevated():

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    boundary_url = 'https://enterprise.mapshare.vic.gov.au/server/rest/services/V_PARCEL_MP/MapServer/0/query'
    metro_topo_url = "https://services6.arcgis.com/GB33F62SbDxJjwEL/ArcGIS/rest/services/Vicmap_Elevation_METRO_1_to_5_metre/FeatureServer/1/query"
    regional_topo_url = "https://enterprise.mapshare.vic.gov.au/server/rest/services/Vicmap_Elevation_STATEWIDE_10_to_20_metre/MapServer/6/query"

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 400000)

    boundary_params = create_parameters_vic(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters_vic(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_params = create_parameters_vic(
        '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)

    vic_e = rh.File3dm()
    vic_e.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(vic_e, "Boundary", (237, 0, 194, 255))
    building_layer_EIndex = create_layer(
        vic_e, "Buildings Elevated", (99, 99, 99, 255))
    topography_layerIndex = create_layer(
        vic_e, "Topography", (191, 191, 191, 255))
    contours_layer_EIndex = create_layer(
        vic_e, "Contours Elevated", (191, 191, 191, 255))


    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            vic_e.Objects.AddCurve(bound_curve, att)

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16       
    
    gh_topography_decoded = encode_ghx_file(r"./gh_scripts/topography.ghx")
    gh_buildings_elevated_decoded = encode_ghx_file(
        r"./gh_scripts/elevate_buildings.ghx")

    bound_curves_list = []
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
                time.sleep(0)
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
                            lon_mapped, lat_mapped = transformer2_vic.transform(
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
                                lon_mapped, lat_mapped = transformer2_vic.transform(
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

    topo_url = regional_topo_url

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
            time.sleep(0)
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
    cen_x, cen_y = transformer2_vic.transform(lon, lat)
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

    cen_x, cen_y = transformer2_vic.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -
                                     centroid.Y, -centroid.Z)

    if bound_curve is not None:
        bound_curve.Translate(translation_vector)

    for obj in vic_e.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "vic_elevated.3dm"
    vic_e.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/carbon', methods=['GET', 'POST'])
def carbon():
    total_carbon = session.get('total_carbon')
    warehouse_carbon = session.get('warehouse_carbon')
    office_carbon = session.get('office_carbon')
    landscaping_carbon = session.get('landscaping_carbon')
    road_cars_carbon = session.get('road_cars_carbon')
    parking_cars_carbon = session.get('parking_cars_carbon')
    road_trucks_carbon = session.get('road_trucks_carbon')
    parking_trucks_carbon = session.get('parking_trucks_carbon')
    gwp = session.get('gwp')
    percentage_change = session.get('percentage_change')
    file_path = session.get('file_path')
    previous_gwp = session.get('previous_gwp')
    gwp_status = session.get('gwp_status')
    delta = session.get('delta')
    color1 = session.get('color1')
    color2 = session.get('color2')
    color3 = session.get('color3')
    color4 = session.get('color4')
    color5 = session.get('color5')
    color6 = session.get('color6')
    color7 = session.get('color7')

    return render_template('carbon.html', total_carbon=total_carbon, warehouse_carbon=warehouse_carbon, office_carbon=office_carbon, gwp=gwp, file_path=file_path, landscaping_carbon=landscaping_carbon, road_cars_carbon=road_cars_carbon, parking_cars_carbon=parking_cars_carbon, road_trucks_carbon=road_trucks_carbon, parking_trucks_carbon=parking_trucks_carbon, previous_gwp=previous_gwp, gwp_status=gwp_status, delta=delta, color1=color1, color2=color2, color3=color3, color4=color4, color5=color5, color6=color6, color7=color7, percentage_change=percentage_change)

@application.route('/get_carbon', methods=['POST'])
def get_carbon():

    new_file = request.files.get('uploadCarbonFile')

    stored_file_path = session.get('file_path')

    if new_file:
        new_file_path = 'tmp/files/' + new_file.filename
        new_file.save(new_file_path)

        if stored_file_path != new_file_path:
            session['file_path'] = new_file_path

    file_path = session.get('file_path')
    if file_path is None:
        return jsonify({'error': True})

    rhFile = rh.File3dm.Read(file_path)
    layers = rhFile.Layers

    shed = []
    office = []
    landscaping = []
    parking_trucks = []
    road_trucks = []
    parking_cars = []
    road_cars = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "WAREHOUSE":
            shed.append(obj)
        if layers[layer_index].Name == "OFFICE":
            office.append(obj)
        if layers[layer_index].Name == "LANDSCAPING":
            landscaping.append(obj)
        if layers[layer_index].Name == "ROAD CARS":
            road_cars.append(obj)
        if layers[layer_index].Name == "PARKING CARS":
            parking_cars.append(obj)
        if layers[layer_index].Name == "ROAD TRUCKS":
            road_trucks.append(obj)
        if layers[layer_index].Name == "PARKING TRUCKS":
            parking_trucks.append(obj)

    shed_breps = [obj.Geometry for obj in shed]
    office_breps = [obj.Geometry for obj in office]
    landscaping_breps = [obj.Geometry for obj in landscaping]
    road_cars_breps = [obj.Geometry for obj in road_cars]
    road_trucks_breps = [obj.Geometry for obj in road_trucks]
    parking_cars_breps = [obj.Geometry for obj in parking_cars]
    parking_trucks_breps = [obj.Geometry for obj in parking_trucks]

    serialized_shed = []
    for brep in shed_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_shed.append(serialized_brep)

    serialized_office = []
    for brep in office_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_office.append(serialized_brep)

    serialized_landscaping = []
    for brep in landscaping_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_landscaping.append(serialized_brep)

    serialized_road_cars = []
    for brep in road_cars_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_road_cars.append(serialized_brep)

    serialized_road_trucks = []
    for brep in road_trucks_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_road_trucks.append(serialized_brep)

    serialized_parking_cars = []
    for brep in parking_cars_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_parking_cars.append(serialized_brep)

    serialized_parking_trucks = []
    for brep in parking_trucks_breps:
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        serialized_parking_trucks.append(serialized_brep)

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

    landscaping_list = [{"ParamName": "Landscaping", "InnerTree": {}}]
    for i, brep in enumerate(serialized_landscaping):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        landscaping_list[0]["InnerTree"][key] = value

    road_cars_list = [{"ParamName": "Road Cars", "InnerTree": {}}]
    for i, brep in enumerate(serialized_road_cars):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        road_cars_list[0]["InnerTree"][key] = value

    road_trucks_list = [{"ParamName": "Road Trucks", "InnerTree": {}}]
    for i, brep in enumerate(serialized_road_trucks):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        road_trucks_list[0]["InnerTree"][key] = value

    parking_trucks_list = [{"ParamName": "Parking Trucks", "InnerTree": {}}]
    for i, brep in enumerate(serialized_parking_trucks):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        parking_trucks_list[0]["InnerTree"][key] = value
    
    parking_cars_list = [{"ParamName": "Parking Cars", "InnerTree": {}}]
    for i, brep in enumerate(serialized_parking_cars):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": brep
            }
        ]
        parking_cars_list[0]["InnerTree"][key] = value
    
    landscaping_c = int(12)
    landscaping_c_list = []
    landscaping_c_list.append(landscaping_c)

    send_landscaping_c_list = [{"ParamName": "Landscaping Carbon", "InnerTree": {}}]
    for i, num in enumerate(landscaping_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_landscaping_c_list[0]["InnerTree"][key] = value

    road_cars_c = float(request.form['roadCarsChoice'])
    road_cars_c_list = []
    road_cars_c_list.append(road_cars_c)

    send_road_cars_c_list = [{"ParamName": "Road Cars Carbon", "InnerTree": {}}]
    for i, num in enumerate(road_cars_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_road_cars_c_list[0]["InnerTree"][key] = value

    road_trucks_c = float(request.form['roadTrucksChoice'])
    road_trucks_c_list = []
    road_trucks_c_list.append(road_trucks_c)

    send_road_trucks_c_list = [{"ParamName": "Road Trucks Carbon", "InnerTree": {}}]
    for i, num in enumerate(road_trucks_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_road_trucks_c_list[0]["InnerTree"][key] = value

    parking_cars_c = float(request.form['parkingCarsChoice'])
    parking_cars_c_list = []
    parking_cars_c_list.append(parking_cars_c)

    send_parking_cars_c_list = [{"ParamName": "Parking Cars Carbon", "InnerTree": {}}]
    for i, num in enumerate(road_cars_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_parking_cars_c_list[0]["InnerTree"][key] = value

    parking_trucks_c = road_cars_c = float(request.form['parkingTrucksChoice'])
    parking_trucks_c_list = []
    parking_trucks_c_list.append(parking_trucks_c)

    send_parking_trucks_c_list = [{"ParamName": "Parking Trucks Carbon", "InnerTree": {}}]
    for i, num in enumerate(parking_trucks_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_parking_trucks_c_list[0]["InnerTree"][key] = value

    roof_c = float(request.form['roofChoice'])
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

    wall_office_c = int(request.form['wallConcrete'])
    wall_office_c_list = []
    wall_office_c_list.append(wall_office_c)

    send_wall_office_c_list = [{"ParamName": "Wall Office Carbon", "InnerTree": {}}]
    for i, num in enumerate(wall_office_c_list):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.Float",
                "data": num
            }
        ]
        send_wall_office_c_list[0]["InnerTree"][key] = value


    gh_carbon = open(r"./gh_scripts/carbon.ghx", mode="r",
                        encoding="utf-8-sig").read()
    gh_carbon_bytes = gh_carbon.encode("utf-8")
    gh_carbon_encoded = base64.b64encode(gh_carbon_bytes)
    gh_carbon_decoded = gh_carbon_encoded.decode("utf-8")

    geo_payload = {
        "algo": gh_carbon_decoded,
        "pointer": None,
        "values": shed_list + office_list + send_roof_c_list + send_slab_c_list + send_wall_c_list + send_landscaping_c_list + send_parking_cars_c_list + send_parking_trucks_c_list + send_road_cars_c_list + send_road_trucks_c_list + landscaping_list + parking_cars_list + parking_trucks_list + road_trucks_list + road_cars_list + send_wall_office_c_list
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper", json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    response_object = json.loads(res.content)['values']

    new_rhFile = rh.File3dm()
    new_rhFile.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    warehouse_layer = rh.Layer()
    warehouse_layer.Name = "Warehouse"
    warehouse_layerIndex = new_rhFile.Layers.Add(warehouse_layer)

    office_layer = rh.Layer()
    office_layer.Name = "Office"
    office_layerIndex = new_rhFile.Layers.Add(office_layer)

    landscape_layer = rh.Layer()
    landscape_layer.Name = "Landscaping"
    landscape_layerIndex = new_rhFile.Layers.Add(landscape_layer)

    road_cars_layer = rh.Layer()
    road_cars_layer.Name = "Road Cars"
    road_cars_layerIndex = new_rhFile.Layers.Add(road_cars_layer)

    road_trucks_layer = rh.Layer()
    road_trucks_layer.Name = "Road Trucks"
    road_trucks_layerIndex = new_rhFile.Layers.Add(road_trucks_layer)

    parking_cars_layer = rh.Layer()
    parking_cars_layer.Name = "Parking Cars"
    parking_cars_layerIndex = new_rhFile.Layers.Add(parking_cars_layer)

    parking_trucks_layer = rh.Layer()
    parking_trucks_layer.Name = "Parking Trucks"
    parking_trucks_layerIndex = new_rhFile.Layers.Add(parking_trucks_layer)

    for val in response_object:
        paramName = val['ParamName']
        if paramName == "RH_OUT:GFA":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        gfa = round(float(json.loads(innerVal['data'])), 2)
        if paramName == "RH_OUT:Warehouse":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        warehouse_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['warehouse_carbon'] = warehouse_carbon
        if paramName == "RH_OUT:Office":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        office_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['office_carbon'] = office_carbon
        if paramName == "RH_OUT:TotalCarbon":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        total_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['total_carbon'] = total_carbon
        if paramName == "RH_OUT:Landscaping":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        landscaping_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['landscaping_carbon'] = landscaping_carbon
        if paramName == "RH_OUT:RoadCars":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        road_cars_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['road_cars_carbon'] = road_cars_carbon
        if paramName == "RH_OUT:RoadTrucks":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        road_trucks_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['road_trucks_carbon'] = road_trucks_carbon
        if paramName == "RH_OUT:ParkingCars":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        parking_cars_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['parking_cars_carbon'] = parking_cars_carbon
        if paramName == "RH_OUT:ParkingTrucks":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        parking_trucks_carbon = round(float(json.loads(innerVal['data'])), 2)
                        session['parking_trucks_carbon'] = parking_trucks_carbon
        if paramName == "RH_OUT:Color1":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color1 = json.loads(innerVal['data'])
                        session['color1'] = color1
        if paramName == "RH_OUT:Color2":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color2 = json.loads(innerVal['data'])
                        session['color2'] = color2
        if paramName == "RH_OUT:Color3":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color3 = json.loads(innerVal['data'])
                        session['color3'] = color3
        if paramName == "RH_OUT:Color4":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color4 = json.loads(innerVal['data'])
                        session['color4'] = color4
        if paramName == "RH_OUT:Color5":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color5 = json.loads(innerVal['data'])
                        session['color5'] = color5
        if paramName == "RH_OUT:Color6":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color6 = json.loads(innerVal['data'])
                        session['color6'] = color6
        if paramName == "RH_OUT:Color7":
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        color7 = json.loads(innerVal['data'])
                        session['color7'] = color7
        if paramName == 'RH_OUT:MeshWarehouse':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = warehouse_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshOffice':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = office_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshLandscaping':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = landscape_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshRoadCars':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = road_cars_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshRoadTrucks':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = road_trucks_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshParkingCars':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = parking_cars_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
        if paramName == 'RH_OUT:MeshParkingTrucks':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = json.loads(innerVal['data'])
                            geo = rh.CommonObject.Decode(data)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = parking_trucks_layerIndex
                            new_rhFile.Objects.AddMesh(geo, att)
    
    gwp = round(float(total_carbon)/float(gfa),2)
    session['gwp'] = gwp

    previous_gwp = session.get('previous_gwp')
    if previous_gwp is not None and previous_gwp != 0:
        delta = round(gwp - previous_gwp, 2)
        percentage_change = abs(round(((gwp - previous_gwp) / previous_gwp) * 100, 1))
        session['percentage_change'] = percentage_change

        session['delta'] = delta
        if delta > 0:
            session['gwp_status'] = 'increase'
        elif delta < 0:
            session['gwp_status'] = 'decrease'
        else:
            session['gwp_status'] = 'unchanged'

    session['previous_gwp'] = gwp

    filename = "carbon_output.3dm"
    new_rhFile.Write('./tmp/files/' + str(filename), 7)
    new_rhFile.Write('./static/' + str(filename), 7)

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
    merged_model.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/environmental', methods=['GET', 'POST'])
def environmental():

    total_sunlight_hours = session.get('total_sunlight_hours', None)
    start_month = session.get('start_month')
    end_month = session.get('end_month')
    start_hour = session.get('start_hour')
    end_hour = session.get('end_hour')

    return render_template('environmental.html', total_sunlight_hours=total_sunlight_hours,
                           start_month=start_month, end_month=end_month,
                           start_hour=start_hour, end_hour=end_hour)

@application.route('/submit_environmental', methods=['POST'])
def submit_environmental():
    file = request.files['uploadFile']
    if file:
        file_path = 'tmp/files/' + file.filename
        file.save(file_path)
    else:
        return jsonify({'error': True})
    
    start_m = int(request.form['Month'])
    end_m = int(request.form['Month'])
    start_h = int(request.form['minHour'])
    end_h = int(request.form['maxHour'])
    
    session['start_month'] = start_m
    session['end_month'] = end_m
    session['start_hour'] = start_h
    session['end_hour'] = end_h

    rhFile = rh.File3dm.Read(file_path)
    layers = rhFile.Layers
    context_list = []
    geometry_list = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Culled Geometry":
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
    start_d_list.append(21)

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
    start_h_list.append(start_h)

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
    end_d_list.append(21)

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
    end_h_list.append(end_h)

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
    
    legendrhFile = rh.File3dm()
    legendrhFile.Settings.ModelUnitSystem = rh.UnitSystem.Meters

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
        elif paramName == 'RH_OUT:legend_mesh':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        legendrhFile.Objects.AddMesh(geo)

    layers = new_rhFile.Layers

    for layer in layers:
        if layer.Name == 'Geometry':
            layer.Visible = False

    filename = "environmental.3dm"
    filename_2 = "environmental_2.3dm"
    new_rhFile.Write('./tmp/files/' + str(filename), 7)
    new_rhFile.Write('./static/' + str(filename), 7)
    legendrhFile.Write('./static/' + str(filename_2), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/tools', methods=['GET', 'POST'])
def tools():
    stream_url = session.get('stream_url')
    return render_template('tools.html', stream_url=stream_url)

@application.route('/submit/speckle', methods=['POST'])
def submitTopo():

    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'
    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})
        
    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
            lat, lon, 30000)
    topo_params = create_parameters(
            '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)
    params = create_parameters('', 'esriGeometryEnvelope',
                                xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    speckle_model = rh.File3dm()
    speckle_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(
        speckle_model, "Boundary", (237, 0, 194, 255))
    lots_layerIndex = create_layer(
        speckle_model, "Lots", (255, 106, 0, 255))
    road_layerIndex = create_layer(
        speckle_model, "Roads", (145, 145, 145, 255))
    building_layerIndex = create_layer(
        speckle_model, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(
        speckle_model, "Contours", (191, 191, 191, 255))
    topography_layerIndex = create_layer(
        speckle_model, "Topography", (191, 191, 191, 255))
    gh_topography_decoded = encode_ghx_file(
        r"./gh_scripts/topography.ghx")

    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")

    lots_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'

    params_dict = {
        lots_url: params,
        topo_url: topo_params
    }
    urls = [
        lots_url,
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
                if url == lots_url:
                    data_dict['lots_data'] = data
                elif url == topo_url:
                    data_dict['topography_data'] = data

    lots_data = data_dict.get('lots_data')
    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            speckle_model.Objects.AddCurve(bound_curve, att)

    add_to_model(lots_data, lots_layerIndex,"plannumber", "Lot Number", speckle_model)

    road_curves = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                road_class = feature['properties']['class']
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
                road_class = feature['properties']['class']
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
                        speckle_model.Objects.AddCurve(geo, att)

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
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        speckle_model.Objects.AddExtrusion(
                            extrusion, att)
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
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            speckle_model.Objects.AddExtrusion(
                                extrusion, att)
                            buildings.append(extrusion)
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
                speckle_model.Objects.AddCurve(curve, att)
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
                    speckle_model.Objects.AddMesh(mesh_geo, att)


    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                        centroid.Y, -centroid.Z)

    if bound_curve is not None:  
        bound_curve.Translate(translation_vector)

    for obj in speckle_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  
            obj.Geometry.Translate(translation_vector)

    filename = "speckle.3dm"
    speckle_model.Write('./tmp/files/' + str(filename), 7)

    streamName = request.form.get('address')
    client = SpeckleClient(host="https://speckle.xyz/")
    account = get_account_from_token(token='091eabd976672b05e476e09b315f8bc1a254de4ca0', server_url="https://speckle.xyz/")

    client.authenticate_with_account(account)

    new_stream_id = client.stream.create(name=f'{streamName}')

    file_path = 'tmp/files/' + filename

    rhFile = rh.File3dm.Read(file_path)
    layers = rhFile.Layers

    topo = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Topography":
            topo.append(obj)

    topo_meshes = [obj.Geometry for obj in topo]

    topo_to_send = [{"ParamName": "Mesh", "InnerTree": {}}]

    for i, mesh in enumerate(topo_meshes):
        serialized_mesh = json.dumps(mesh, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Mesh",
                "data": serialized_mesh
            }
        ]
        topo_to_send[0]["InnerTree"][key] = value

    buildings = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Buildings":
            buildings.append(obj)

    buildings_list = [obj.Geometry for obj in buildings]

    buildings_to_send = [{"ParamName": "Buildings", "InnerTree": {}}]

    for i, brep in enumerate(buildings_list):
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": serialized_brep
            }
        ]
        buildings_to_send[0]["InnerTree"][key] = value

    elevated_buildings = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Buildings Elevated":
            elevated_buildings.append(obj)

    elevated_buildings_list = [obj.Geometry for obj in elevated_buildings]

    elevated_buildings_to_send = [{"ParamName": "ElevatedBuildings", "InnerTree": {}}]

    for i, brep in enumerate(elevated_buildings_list):
        serialized_brep = json.dumps(brep, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Brep",
                "data": serialized_brep
            }
        ]
        elevated_buildings_to_send[0]["InnerTree"][key] = value

    contours = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Contours":
            contours.append(obj)

    contours_list = [obj.Geometry for obj in contours]

    contours_to_send = [{"ParamName": "Contours", "InnerTree": {}}]

    for i, curve in enumerate(contours_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        contours_to_send[0]["InnerTree"][key] = value

    roads = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Roads":
            roads.append(obj)

    roads_list = [obj.Geometry for obj in roads]

    roads_to_send = [{"ParamName": "Roads", "InnerTree": {}}]

    for i, curve in enumerate(roads_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        roads_to_send[0]["InnerTree"][key] = value

    lots = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Lots":
            lots.append(obj)

    lots_list = [obj.Geometry for obj in lots]

    lots_to_send = [{"ParamName": "Lots", "InnerTree": {}}]

    for i, curve in enumerate(lots_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        lots_to_send[0]["InnerTree"][key] = value

    isochrones = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Walking Isochrone" or layers[layer_index].Name == "Cycling Isochrone" or layers[layer_index].Name == "Driving Isochrone":
            isochrones.append(obj)

    isochrones_list = [obj.Geometry for obj in isochrones]

    isochrones_to_send = [{"ParamName": "Isochrone", "InnerTree": {}}]

    for i, curve in enumerate(isochrones_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        isochrones_to_send[0]["InnerTree"][key] = value

    parks = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Parks":
            parks.append(obj)

    parks_list = [obj.Geometry for obj in parks]

    parks_to_send = [{"ParamName": "Parks", "InnerTree": {}}]

    for i, curve in enumerate(parks_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        parks_to_send[0]["InnerTree"][key] = value

    heritage = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Heritage":
            heritage.append(obj)

    heritage_list = [obj.Geometry for obj in heritage]

    heritage_to_send = [{"ParamName": "Heritage", "InnerTree": {}}]

    for i, curve in enumerate(heritage_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        heritage_to_send[0]["InnerTree"][key] = value

    flood = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Flood":
            flood.append(obj)

    flood_list = [obj.Geometry for obj in flood]

    flood_to_send = [{"ParamName": "Flood", "InnerTree": {}}]

    for i, curve in enumerate(flood_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        flood_to_send[0]["InnerTree"][key] = value

    native = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Native Land":
            native.append(obj)

    native_list = [obj.Geometry for obj in native]

    native_to_send = [{"ParamName": "Native", "InnerTree": {}}]

    for i, curve in enumerate(native_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        native_to_send[0]["InnerTree"][key] = value

    admin = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Administrative Boundaries":
            admin.append(obj)

    admin_list = [obj.Geometry for obj in admin]

    admin_to_send = [{"ParamName": "Admin", "InnerTree": {}}]

    for i, curve in enumerate(admin_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        admin_to_send[0]["InnerTree"][key] = value

    bushfire = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Bushfire":
            bushfire.append(obj)

    bushfire_list = [obj.Geometry for obj in bushfire]

    bushfire_to_send = [{"ParamName": "Bushfire", "InnerTree": {}}]

    for i, curve in enumerate(bushfire_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        bushfire_to_send[0]["InnerTree"][key] = value

    boundary = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Boundary":
            boundary.append(obj)

    boundary_list = [obj.Geometry for obj in boundary]

    boundary_to_send = [{"ParamName": "Boundary", "InnerTree": {}}]

    for i, curve in enumerate(boundary_list):
        serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
        key = f"{{{i};0}}"
        value = [
            {
                "type": "Rhino.Geometry.Curve",
                "data": serialized_curve
            }
        ]
        boundary_to_send[0]["InnerTree"][key] = value

    input_streams = []
    input_streams.append(new_stream_id)

    url_to_send = [{"ParamName": "StreamID", "InnerTree": {}}]

    for i, url in enumerate(input_streams):
        key = f"{{{i};0}}"
        value = [
            {
                "type": "System.String",
                "data": url
            }
        ]
        url_to_send[0]["InnerTree"][key] = value


    gh_decoded = encode_ghx_file('./gh_scripts/topoSpeckle.ghx')

    geo_payload = {
        "algo": gh_decoded,
        "pointer": None,
        "values": topo_to_send + url_to_send + buildings_to_send + elevated_buildings_to_send + contours_to_send + roads_to_send + lots_to_send + isochrones_to_send + parks_to_send + heritage_to_send + flood_to_send + admin_to_send + native_to_send + bushfire_to_send + boundary_to_send
    }

    counter = 0
    while True:
        res = requests.post(compute_url + "grasshopper", json=geo_payload, headers=headers)
        if res.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    
    stream_url = 'https://speckle.xyz/streams/' + str(new_stream_id)

    session['stream_url'] = stream_url

    return jsonify({"stream_id": new_stream_id})

@application.route('/submit/images', methods=['POST'])
def submitImages():
    boundary_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'
    topo_url = 'https://portal.spatial.nsw.gov.au/server/rest/services/NSW_Elevation_and_Depth_Theme/MapServer/2/query'
    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})
        
    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    boundary_params = create_parameters(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
            lat, lon, 30000)
    topo_params = create_parameters(
            '', 'esriGeometryEnvelope', t_xmin_LL, t_ymin_LL, t_xmax_LL, t_ymax_LL)
    params = create_parameters('', 'esriGeometryEnvelope',
                                xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    images_model = rh.File3dm()
    images_model.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(
        images_model, "Boundary", (237, 0, 194, 255))
    lots_layerIndex = create_layer(
        images_model, "Lots", (255, 106, 0, 255))
    road_layerIndex = create_layer(
        images_model, "Roads", (145, 145, 145, 255))
    building_layerIndex = create_layer(
        images_model, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(
        images_model, "Contours", (191, 191, 191, 255))
    topography_layerIndex = create_layer(
        images_model, "Topography", (191, 191, 191, 255))
    gh_topography_decoded = encode_ghx_file(
        r"./gh_scripts/topography.ghx")

    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")

    lots_url = 'https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Cadastre/MapServer/9/query'

    params_dict = {
        lots_url: params,
        topo_url: topo_params
    }
    urls = [
        lots_url,
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
                if url == lots_url:
                    data_dict['lots_data'] = data
                elif url == topo_url:
                    data_dict['topography_data'] = data

    lots_data = data_dict.get('lots_data')
    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            images_model.Objects.AddCurve(bound_curve, att)

    add_to_model(lots_data, lots_layerIndex,"plannumber", "Lot Number", images_model)

    road_curves = []
    for tile in tiles:
        mb_data = concurrent_fetching(zoom, tile)
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

        road_layer = tiles1['road']

        tile1 = mercantile.Tile(tile.x, tile.y, 16)
        bbox = mercantile.bounds(tile1)
        lon1, lat1, lon2, lat2 = bbox

        for feature in road_layer['features']:
            geometry_type = feature['geometry']['type']
            if geometry_type == 'LineString':
                geometry = feature['geometry']['coordinates']
                road_class = feature['properties']['class']
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
                road_class = feature['properties']['class']
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
                        images_model.Objects.AddCurve(geo, att)

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
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        images_model.Objects.AddExtrusion(
                            extrusion, att)
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
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            images_model.Objects.AddExtrusion(
                                extrusion, att)
                            buildings.append(extrusion)
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
                images_model.Objects.AddCurve(curve, att)
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
                    images_model.Objects.AddMesh(mesh_geo, att)


    cen_x, cen_y = transformer2.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)

    translation_vector = rh.Vector3d(-centroid.X, -
                                        centroid.Y, -centroid.Z)

    if bound_curve is not None:  
        bound_curve.Translate(translation_vector)

    for obj in images_model.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:  
            obj.Geometry.Translate(translation_vector)

    filename = "images.3dm"
    images_model.Write('./tmp/files/' + str(filename), 7)

    file_path = 'tmp/files/' + filename
    
    rhFile = rh.File3dm.Read(file_path)
    layers = rhFile.Layers

    admin = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Administrative Boundaries":
            admin.append(obj)

    admin_curves = [obj.Geometry for obj in admin]

    admin_values = []
    for obj in admin:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            admin_values.append(vals)

    zoning = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Zoning":
            zoning.append(obj)

    zoning_curves = [obj.Geometry for obj in zoning]

    zoning_values = []
    for obj in zoning:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            zoning_values.append(vals)

    hob = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "HoB":
            hob.append(obj)

    hob_curves = [obj.Geometry for obj in hob]

    hob_values = []
    for obj in hob:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            hob_values.append(vals)

    mls = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Minimum Lot Size":
            mls.append(obj)

    mls_curves = [obj.Geometry for obj in mls]

    mls_values = []
    for obj in mls:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            mls_values.append(vals)

    fsr = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "FSR":
            fsr.append(obj)

    fsr_curves = [obj.Geometry for obj in fsr]

    fsr_values = []
    for obj in fsr:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            fsr_values.append(vals)

    native = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Native Land":
            native.append(obj)

    native_curves = [obj.Geometry for obj in native]

    native_values = []
    for obj in native:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            native_values.append(vals)

    boundary = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Boundary":
            boundary.append(obj)

    boundary_curves = [obj.Geometry for obj in boundary]

    driving_isochrone = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Driving Isochrone":
            driving_isochrone.append(obj)

    driving_isochrone_curves = [obj.Geometry for obj in driving_isochrone]

    walking_isochrone = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Walking Isochrone":
            walking_isochrone.append(obj)

    walking_isochrone_curves = [obj.Geometry for obj in walking_isochrone]

    cycling_isochrone = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Cycling Isochrone":
            cycling_isochrone.append(obj)

    cycling_isochrone_curves = [obj.Geometry for obj in cycling_isochrone]

    lots = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Lots":
            lots.append(obj)

    lots_curves = [obj.Geometry for obj in lots]

    plan_extent = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Plan Extent":
            plan_extent.append(obj)

    plan_extent_curves = [obj.Geometry for obj in plan_extent]

    roads = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Roads":
            roads.append(obj)

    roads_curves = [obj.Geometry for obj in roads]

    heritage = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Heritage":
            heritage.append(obj)

    heritage_curves = [obj.Geometry for obj in heritage]

    parks = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Parks":
            parks.append(obj)

    parks_curves = [obj.Geometry for obj in parks]

    parks_values = []
    for obj in parks:
        user_strings = obj.Attributes.GetUserStrings()
        for user_string in user_strings:
            vals = user_string[1]
            parks_values.append(vals)

    buildings = []
    for obj in rhFile.Objects:
        layer_index = obj.Attributes.LayerIndex
        if layers[layer_index].Name == "Building Footprint":
            buildings.append(obj)

    building_curves = [obj.Geometry for obj in buildings]

    def s_b_compute(breps, fileName, ghx_file_path, pngName):
        list = [{"ParamName": "Geometry", "InnerTree": {}}]
        for i, mesh in enumerate(breps):
            serialized_mesh = json.dumps(mesh, cls=__Rhino3dmEncoder)
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "Rhino.Geometry.Brep",
                    "data": serialized_mesh
                }
            ]
            list[0]["InnerTree"][key] = value


        file_name_list = []
        file_name_list.append(fileName)

        filename_send = [{"ParamName": "FileName", "InnerTree": {}}]
        for i, val in enumerate(file_name_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            filename_send[0]["InnerTree"][key] = value

        folder_name_list = []
        folder_name_list.append('folder')

        foldername_send = [{"ParamName": "FolderName", "InnerTree": {}}]
        for i, val in enumerate(folder_name_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            foldername_send[0]["InnerTree"][key] = value


        gh_graphics = open(ghx_file_path, mode="r",
                        encoding="utf-8-sig").read()
        gh_graphics_bytes = gh_graphics.encode("utf-8")
        gh_graphics_encoded = base64.b64encode(gh_graphics_bytes)
        gh_graphics_decoded = gh_graphics_encoded.decode("utf-8")

        geo_payload = {
            "algo": gh_graphics_decoded,
            "pointer": None,
            "values":  list + filename_send + foldername_send
        }

        counter = 0
        while True:
            res = requests.post(compute_url + "grasshopper", json=geo_payload, headers=headers)
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
            if paramName == 'RH_OUT:StringImage':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = innerVal['data']
                            encoded_image = data
                            decoded_image = base64.b64decode(encoded_image)
                            image = Image.open(BytesIO(decoded_image))
                            image.save(f'./tmp/files/images/{pngName}.png')

        return None

    def s_compute(meshes, vals_list, fileName, ghx_file_path, pngName):
        list = [{"ParamName": "Geometry", "InnerTree": {}}]
        for i, mesh in enumerate(meshes):
            serialized_mesh = json.dumps(mesh, cls=__Rhino3dmEncoder)
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "Rhino.Geometry.Mesh",
                    "data": serialized_mesh
                }
            ]
            list[0]["InnerTree"][key] = value

        val_list_send = [{"ParamName": "Vals", "InnerTree": {}}]
        for i, val in enumerate(vals_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            val_list_send[0]["InnerTree"][key] = value


        file_name_list = []
        file_name_list.append(fileName)

        filename_send = [{"ParamName": "FileName", "InnerTree": {}}]
        for i, val in enumerate(file_name_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            filename_send[0]["InnerTree"][key] = value

        folder_name_list = []
        folder_name_list.append('folder')

        foldername_send = [{"ParamName": "FolderName", "InnerTree": {}}]
        for i, val in enumerate(folder_name_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            foldername_send[0]["InnerTree"][key] = value


        gh_graphics = open(ghx_file_path, mode="r",
                        encoding="utf-8-sig").read()
        gh_graphics_bytes = gh_graphics.encode("utf-8")
        gh_graphics_encoded = base64.b64encode(gh_graphics_bytes)
        gh_graphics_decoded = gh_graphics_encoded.decode("utf-8")

        geo_payload = {
            "algo": gh_graphics_decoded,
            "pointer": None,
            "values":  list + val_list_send + filename_send + foldername_send
        }

        counter = 0
        while True:
            res = requests.post(compute_url + "grasshopper", json=geo_payload, headers=headers)
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
            if paramName == 'RH_OUT:StringImage':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = innerVal['data']
                            encoded_image = data
                            decoded_image = base64.b64decode(encoded_image)
                            image = Image.open(BytesIO(decoded_image))
                            image.save(f'./tmp/files/images/{pngName}.png')
            
        return None

    def s_l_compute(curves, fileName, ghx_file_path, pngName):
        serialized_curves = []
        for curve in curves:
            serialized_curve = json.dumps(curve, cls=__Rhino3dmEncoder)
            serialized_curves.append(serialized_curve)

        list_send = [{"ParamName": "Geometry", "InnerTree": {}}]
        for i, curve in enumerate(serialized_curves):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "Rhino.Geometry.Curve",
                    "data": curve
                }
            ]
            list_send[0]["InnerTree"][key] = value

        file_name_list = []
        file_name_list.append(fileName)

        filename_send = [{"ParamName": "FileName", "InnerTree": {}}]
        for i, val in enumerate(file_name_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            filename_send[0]["InnerTree"][key] = value

        folder_name_list = []
        folder_name_list.append('folder')

        foldername_send = [{"ParamName": "FolderName", "InnerTree": {}}]
        for i, val in enumerate(folder_name_list):
            key = f"{{{i};0}}"
            value = [
                {
                    "type": "System.String",
                    "data": val
                }
            ]
            foldername_send[0]["InnerTree"][key] = value

        gh_graphics = open(ghx_file_path, mode="r",
                        encoding="utf-8-sig").read()
        gh_graphics_bytes = gh_graphics.encode("utf-8")
        gh_graphics_encoded = base64.b64encode(gh_graphics_bytes)
        gh_graphics_decoded = gh_graphics_encoded.decode("utf-8")

        geo_payload = {
            "algo": gh_graphics_decoded,
            "pointer": None,
            "values": list_send + filename_send + foldername_send
        }

        counter = 0
        while True:
            res = requests.post(compute_url + "grasshopper", json=geo_payload, headers=headers)
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
            if paramName == 'RH_OUT:StringImage':
                innerTree = val['InnerTree']
                for key, innerVals in innerTree.items():
                    for innerVal in innerVals:
                        if 'data' in innerVal:
                            data = innerVal['data']
                            encoded_image = data
                            decoded_image = base64.b64decode(encoded_image)
                            image = Image.open(BytesIO(decoded_image))
                            image.save(f'./tmp/files/images/{pngName}.png')
            
        return None
    
    s_compute(admin_curves, admin_values, 'Admin', './gh_scripts/adminColors.ghx', '10KM_Administrative Boundaries')
    # 10km
    s_compute(admin_curves, admin_values, 'Admin', './gh_scripts/adminColors.ghx', '10KM_Administrative Boundaries')
    # 10km
    s_compute(zoning_curves, zoning_values, 'Zoning','./gh_scripts/zoningColors.ghx','1KM_Zoning')
    # 1km
    s_compute(hob_curves, hob_values, 'HoB','./gh_scripts/hobColors.ghx','1KM_HOB')
    # 1km
    s_compute(mls_curves, mls_values, 'MLS','./gh_scripts/mlsColors.ghx','1KM_MLS')
    # 1km
    s_compute(fsr_curves, fsr_values, 'FSR','./gh_scripts/fsrColors.ghx','1KM_FSR')
    # 1km
    s_compute(native_curves, native_values, 'Native Land','./gh_scripts/nativeColors.ghx','10KM_Native')
    # 10km
    s_compute(parks_curves, parks_values, 'Parks', './gh_scripts/parksColors.ghx','10KM_Parks')
    # 10km
    s_l_compute(boundary_curves, 'Boundary', './gh_scripts/1km_lines.ghx','1KM_Boundary')
    # 1km
    s_l_compute(driving_isochrone_curves, 'Driving Isochrone', './gh_scripts/10km_lines.ghx','10KM_Driving Isochrone')
    # 10km
    s_l_compute(walking_isochrone_curves, 'Walking Isochrone', './gh_scripts/1km_lines.ghx','1KM_Walking Isochrone')
    # 1km
    s_l_compute(cycling_isochrone_curves, 'Cycling Isochrone', './gh_scripts/10km_lines.ghx', '10KM_Cycling Isochrone')
    # 10km
    s_l_compute(lots_curves, 'Lots', './gh_scripts/1km_lines.ghx','1KM_Lots')
    # 1km
    s_l_compute(plan_extent_curves, 'Plan Extent', './gh_scripts/1km_lines.ghx','1KM_Plan Extent')
    # 1km
    s_l_compute(roads_curves, 'Roads', './gh_scripts/1km_lines.ghx','1KM_Roads')
    # 1km
    s_l_compute(heritage_curves, 'Heritage', './gh_scripts/1km_lines.ghx','1KM_Heritage')
    # 1km
    s_b_compute(building_curves, 'Buildings','./gh_scripts/buildingsColor.ghx', '1KM_Buildings')
    # 1km

    directory = './tmp/files/images'
    files = os.listdir(directory)

    with zipfile.ZipFile('zipfile.zip', 'w') as zip:
        for file in files:
            file_path = os.path.join(directory, file)
            if os.path.isfile(file_path):
                zip.write(file_path, os.path.basename(file_path))

    return send_from_directory('.', 'zipfile.zip', as_attachment=True)

@application.route('/tas', methods=['POST', 'GET'])
def tas():
    return render_template('tas.html', lat=-42.880554, lon=147.324997)

@application.route('/tas_planning', methods=['POST'])
def tas_planning():
    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    tas_planning = rh.File3dm()
    tas_planning.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_url = 'https://services.thelist.tas.gov.au/arcgis/rest/services/Public/PlanningOnline/MapServer/2/query'
    zoning_url = 'https://services.thelist.tas.gov.au/arcgis/rest/services/Public/PlanningOnline/MapServer/4/query'
    adminboundaries_url = 'https://services.thelist.tas.gov.au/arcgis/rest/services/Public/CadastreAndAdministrative/MapServer/7/query'
    heritage_url = 'https://services.thelist.tas.gov.au/arcgis/rest/services/HT/HT_Public/MapServer/0/query'
    native_url = 'https://native-land.ca/wp-json/nativeland/v1/api/index.php'

    gh_zoning_decoded = encode_ghx_file(r"./gh_scripts/vic_qld_zoning.ghx")
    gh_interpolate_decoded = encode_ghx_file(r"./gh_scripts/interpolate.ghx")
    gh_roads_decoded = encode_ghx_file(r"./gh_scripts/roads.ghx")
    gh_raster_decoded = encode_ghx_file(r"./gh_scripts/image.ghx")

    boundary_layerIndex = create_layer(tas_planning, "Boundary", (237, 0, 194, 255))
    admin_layerIndex = create_layer(
        tas_planning, "Administrative Boundaries", (134, 69, 255, 255))
    native_layerIndex = create_layer(tas_planning, "Native Land", (134, 69, 255, 255))
    zoning_layerIndex = create_layer(tas_planning, "Zoning", (255, 180, 18, 255))
    lots_layerIndex = create_layer(tas_planning, "Lots", (255, 106, 0, 255))
    road_layerIndex = create_layer(tas_planning, "Roads", (145, 145, 145, 255))
    walking_layerIndex = create_layer(
        tas_planning, "Walking Isochrone", (129, 168, 0, 255))
    cycling_layerIndex = create_layer(
        tas_planning, "Cycling Isochrone", (0, 168, 168, 255))
    driving_layerIndex = create_layer(
        tas_planning, "Driving Isochrone", (168, 0, 121, 255))
    heritage_layerIndex = create_layer(tas_planning, "Heritage", (153, 153, 153, 255))
    raster_layerIndex = create_layer(tas_planning, "Raster", (153, 153, 153, 255))
    
    l_xmin_LL, l_xmax_LL, l_ymin_LL, l_ymax_LL = create_boundary(lat, lon, 10000)
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

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    l_xmin_LL, l_xmax_LL, l_ymin_LL, l_ymax_LL = create_boundary(
        lat, lon, 10000)
    n_xmin_LL, n_xmax_LL, n_ymin_LL, n_ymax_LL = create_boundary(
        lat, lon, 800000)

    boundary_params = create_parameters_vic(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters_vic(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            tas_planning.Objects.AddCurve(bound_curve, att)

    counter = 0
    while True:
        native_response = requests.post(native_url, json=native_post)
        if native_response.status_code == 200:
            break
        else:
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
                native_x, native_y = transformer2_vic.transform(
                    coord[0], coord[1])
                point = rh.Point3d(native_x, native_y, 0)
                points.append(point)
            polyline = rh.Polyline(points)
            curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = native_layerIndex
            att.SetUserString("Native Name", str(name))
            tas_planning.Objects.AddCurve(curve, att)

    counter = 0
    while True:
        admin_response = requests.get(adminboundaries_url, params=params)
        if admin_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    admin_data = json.loads(admin_response.text)
    if "features" in admin_data:
        for feature in admin_data["features"]:
            suburb_name = feature['attributes']['NAME']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                att = rh.ObjectAttributes()
                att.LayerIndex = admin_layerIndex
                att.SetUserString("Suburb Name", str(suburb_name))
                tas_planning.Objects.AddCurve(curve, att)
    else:
        time.sleep(0)

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
            time.sleep(0)
    zoning_data = json.loads(zoning_response.text)
    if "features" in zoning_data:
        for feature in zoning_data["features"]:
            zoning_code = feature['attributes']['ZONE']
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
            time.sleep(0)
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
                        tas_planning.Objects.AddMesh(geo, att)
                        i += 1

    counter = 0
    while True:
        lots_response = requests.get(boundary_url, params=l_params)
        if lots_response.status_code == 200:
            break
        else:
            counter += 1
            if counter >= 3:
                return jsonify({'error': True})
            time.sleep(0)
    lots_data = json.loads(lots_response.text)
    if "features" in lots_data:
        for feature in lots_data["features"]:
            lot_number = feature['attributes']['PID']
            geometry = feature["geometry"]
            for ring in geometry["rings"]:
                points = []
                for coord in ring:
                    point = rh.Point3d(
                        coord[0], coord[1], 0)
                    points.append(point)
                polyline = rh.Polyline(points)
                curve = polyline.ToNurbsCurve()
                att = rh.ObjectAttributes()
                att.SetUserString("Lot ID", str(lot_number))
                att.LayerIndex = lots_layerIndex
                tas_planning.Objects.AddCurve(curve, att)

    else:
        time.sleep(0)

    heritage_data = get_data(heritage_url, params)
    add_to_model(heritage_data, heritage_layerIndex,
                 "THR_NAME", "Heritage Name", tas_planning)

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
                time.sleep(0)
        mb_data = mb_response.content
        tiles1 = mapbox_vector_tile.decode(mb_data)

        if 'road' not in tiles1:
            continue

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
                    lon_mapped, lat_mapped = transformer2_vic.transform(
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
                        lon_mapped, lat_mapped = transformer2_vic.transform(
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
            time.sleep(0)
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
                        tas_planning.Objects.AddCurve(geo, att)

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
                        tas_planning.Objects.AddCurve(geo, att)

    add_curves_to_model(walking_data, transformer2_vic, walking_layerIndex, tas_planning)
    add_curves_to_model(cycling_data, transformer2_vic, cycling_layerIndex, tas_planning)
    add_curves_to_model(driving_data, transformer2_vic, driving_layerIndex, tas_planning)

    ras_xmin_LL, ras_xmax_LL, ras_ymin_LL, ras_ymax_LL = create_boundary(lat, lon, 1000)

    ras_tiles = list(mercantile.tiles(ras_xmin_LL, ras_ymin_LL, ras_xmax_LL, ras_ymax_LL, zooms=16))

    for tile in ras_tiles:
        mb_url = f"https://api.mapbox.com/v4/mapbox.satellite/{zoom}/{tile.x}/{tile.y}@2x.png256?access_token={mapbox_access_token}"
        response = requests.get(mb_url)

        if response.status_code == 200:
            image_data = BytesIO(response.content)
            image = Image.open(image_data)
            file_name = "ras.png"
            image.save('./tmp/' + file_name)

    rastile = ras_tiles[0] 

    bbox = mercantile.bounds(rastile)
    lon1, lat1, lon2, lat2 = bbox
    t_lon1, t_lat1 = transformer2_vic.transform(lon1, lat1)
    t_lon2, t_lat2 = transformer2_vic.transform(lon2, lat2)

    raster_points = [
        rh.Point3d(t_lon1, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat1, 0),
        rh.Point3d(t_lon2, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat2, 0),
        rh.Point3d(t_lon1, t_lat1, 0)
    ]

    points_list = rh.Point3dList(raster_points)
    raster_curve = rh.PolylineCurve(points_list)
    raster_curve = raster_curve.ToNurbsCurve()

    with open('./tmp/' + file_name, 'rb') as img_file:
        img_bytes = img_file.read()

    b64_string = base64.b64encode(img_bytes).decode('utf-8')

    string_encoded = b64_string
    send_string = [{"ParamName": "BaseString", "InnerTree": {}}]

    serialized_string = json.dumps(string_encoded, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "System.String",
            "data": serialized_string
        }
    ]
    send_string[0]["InnerTree"][key] = value

    curve_payload = [{"ParamName": "Curve", "InnerTree": {}}]
    serialized_curve = json.dumps(raster_curve, cls=__Rhino3dmEncoder)
    key = "{0};0".format(0)
    value = [
        {
            "type": "Rhino.Geometry.Curve",
            "data": serialized_curve
        }
    ]
    curve_payload[0]["InnerTree"][key] = value

    geo_payload = {
        "algo": gh_raster_decoded,
        "pointer": None,
        "values": send_string + curve_payload
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
                    att.LayerIndex = raster_layerIndex
                    tas_planning.Objects.AddMesh(geo, att)

    cen_x, cen_y = transformer2_vic.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -centroid.Y, -centroid.Z)

    if bound_curve is not None:
        bound_curve.Translate(translation_vector)

    for obj in tas_planning.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "tas_planning.3dm"
    tas_planning.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

@application.route('/tas_geometry', methods=['POST'])
def tas_geometry():
    boundary_url = 'https://services.thelist.tas.gov.au/arcgis/rest/services/Public/PlanningOnline/MapServer/2/query'
    topo_url = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/TopographyAndRelief/MapServer/13/query"

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    tas_g = rh.File3dm()
    tas_g.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerIndex = create_layer(tas_g, "Boundary", (237, 0, 194, 255))
    building_layerIndex = create_layer(tas_g, "Buildings", (99, 99, 99, 255))
    contours_layerIndex = create_layer(tas_g, "Contours", (191, 191, 191, 255))
    geometry_layerIndex = create_layer(tas_g, "Geometry", (191, 191, 191, 255))
    buildingfootprint_LayerIndex = create_layer(tas_g, "Building Footprint", (191, 191, 191, 255))

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)

    boundary_params = create_parameters_vic(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters_vic(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters_vic(
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

    topography_data = data_dict.get('topography_data')

    tiles = list(mercantile.tiles(
        xmin_LL, ymin_LL, xmax_LL, ymax_LL, zooms=16))
    zoom = 16

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerIndex
            att.SetUserString("Address", str(address))
            tas_g.Objects.AddCurve(bound_curve, att)

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
                            lon_mapped, lat_mapped = transformer2_vic.transform(
                                lon_mapped, lat_mapped)
                            point = rh.Point3d(
                                lon_mapped, lat_mapped, 0)
                            points.append(point)
                        polyline = rh.Polyline(points)
                        curve = polyline.ToNurbsCurve()
                        orientation = curve.ClosedCurveOrientation()
                        if str(orientation) == 'CurveOrientation.Clockwise':
                            curve.Reverse()
                        att_bf = rh.ObjectAttributes()
                        att_bf.LayerIndex = buildingfootprint_LayerIndex
                        tas_g.Objects.AddCurve(curve, att_bf)
                        extrusion = rh.Extrusion.Create(
                            curve, height, True)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = building_layerIndex
                        att.SetUserString(
                            "Building Height", str(height))
                        tas_g.Objects.AddExtrusion(
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
                                lon_mapped, lat_mapped = transformer2_vic.transform(
                                    lon_mapped, lat_mapped)
                                point = rh.Point3d(
                                    lon_mapped, lat_mapped, 0)
                                points.append(point)
                            polyline = rh.Polyline(points)
                            curve = polyline.ToNurbsCurve()
                            orientation = curve.ClosedCurveOrientation()
                            if str(orientation) == 'CurveOrientation.Clockwise':
                                curve.Reverse()
                            att_bf = rh.ObjectAttributes()
                            att_bf.LayerIndex = buildingfootprint_LayerIndex
                            tas_g.Objects.AddCurve(curve, att_bf)
                            extrusion = rh.Extrusion.Create(
                                curve, height, True)
                            att = rh.ObjectAttributes()
                            att.LayerIndex = building_layerIndex
                            att.SetUserString(
                                "Building Height", str(height))
                            tas_g.Objects.AddExtrusion(
                                extrusion, att)
        else:
            time.sleep(0)

    if "features" in topography_data:
        for feature in topography_data["features"]:
            elevation = feature['attributes']['ELEVATION']
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
                tas_g.Objects.AddCurve(curve, att)
    else:
        time.sleep(0)
        

    cen_x, cen_y = transformer2_vic.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -centroid.Y, -centroid.Z)

    if bound_curve is not None:
        bound_curve.Translate(translation_vector)

    for obj in tas_g.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "tas_geometry.3dm"
    tas_g.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)


@application.route('/tas_elevated', methods=['POST'])
def tas_elevated():
    boundary_url = 'https://services.thelist.tas.gov.au/arcgis/rest/services/Public/PlanningOnline/MapServer/2/query'
    topo_url = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/TopographyAndRelief/MapServer/13/query"

    address = request.form.get('address')
    arcgis_geocoder_url = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/find"
    params = {
        "text": address,
        "f": "json",
        "outFields": "Location"
    }

    response = requests.get(arcgis_geocoder_url, params=params)

    if response.status_code == 200:
        data = response.json()
        if "locations" in data and len(data["locations"]) > 0:
            location = data["locations"][0]
            lon = location["feature"]["geometry"]["x"]
            lat = location["feature"]["geometry"]["y"]
        else:
            return jsonify({'error': True})

    tas_e = rh.File3dm()
    tas_e.Settings.ModelUnitSystem = rh.UnitSystem.Meters

    boundary_layerEIndex = create_layer(tas_e, "Boundary Elevated", (237, 0, 194, 255))
    building_layer_EIndex = create_layer(
        tas_e, "Buildings Elevated", (99, 99, 99, 255))
    topography_layerIndex = create_layer(
        tas_e, "Topography", (191, 191, 191, 255))
    contours_layer_EIndex = create_layer(
        tas_e, "Contours Elevated", (191, 191, 191, 255))

    gh_topography_decoded = encode_ghx_file(r"./gh_scripts/topography.ghx")
    gh_buildings_elevated_decoded = encode_ghx_file(
        r"./gh_scripts/elevate_buildings.ghx")

    xmin_LL, xmax_LL, ymin_LL, ymax_LL = create_boundary(lat, lon, 10000)
    t_xmin_LL, t_xmax_LL, t_ymin_LL, t_ymax_LL = create_boundary(
        lat, lon, 30000)

    boundary_params = create_parameters_vic(
        f'{lon},{lat}', 'esriGeometryPoint', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    params = create_parameters_vic(
        '', 'esriGeometryEnvelope', xmin_LL, ymin_LL, xmax_LL, ymax_LL)
    topo_params = create_parameters_vic(
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

    topography_data = data_dict.get('topography_data')

    counter = 0
    while True:
        response = requests.get(boundary_url, boundary_params)
        if response.status_code == 200:
            boundary_data = json.loads(response.text)
            if boundary_data["features"]:
                break
        else:
            counter += 1
            if counter >= 5:
                return jsonify({'error': True})
            time.sleep(0)

    for feature in boundary_data["features"]:
        geometry = feature["geometry"]
        for ring in geometry["rings"]:
            points = []
            for coord in ring:
                point = rh.Point3d(coord[0], coord[1], 0)
                points.append(point)
            polyline = rh.Polyline(points)
            bound_curve = polyline.ToNurbsCurve()
            att = rh.ObjectAttributes()
            att.LayerIndex = boundary_layerEIndex
            att.SetUserString("Address", str(address))
            tas_e.Objects.AddCurve(bound_curve, att)

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
                            lon_mapped, lat_mapped = transformer2_vic.transform(
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
                                lon_mapped, lat_mapped = transformer2_vic.transform(
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
            elevation = feature['attributes']['ELEVATION']
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
                tas_e.Objects.AddCurve(
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
    cen_x, cen_y = transformer2_vic.transform(lon, lat)
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
                    tas_e.Objects.AddMesh(mesh_geo, att)

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
                        tas_e.Objects.AddBrep(geo, att)
        elif paramName == 'RH_OUT:UpBound':
            innerTree = val['InnerTree']
            for key, innerVals in innerTree.items():
                for innerVal in innerVals:
                    if 'data' in innerVal:
                        data = json.loads(innerVal['data'])
                        geo = rh.CommonObject.Decode(data)
                        att = rh.ObjectAttributes()
                        att.LayerIndex = boundary_layerEIndex
                        tas_e.Objects.AddCurve(geo, att)

    cen_x, cen_y = transformer2_vic.transform(lon, lat)
    centroid = rh.Point3d(cen_x, cen_y, 0)
    translation_vector = rh.Vector3d(-centroid.X, -centroid.Y, -centroid.Z)

    if bound_curve is not None:
        bound_curve.Translate(translation_vector)

    for obj in tas_e.Objects:
        if obj.Geometry != bound_curve and obj.Geometry is not None:
            obj.Geometry.Translate(translation_vector)

    filename = "tas_elevated.3dm"
    tas_e.Write('./tmp/files/' + str(filename), 7)

    return send_from_directory('./tmp/files/', filename, as_attachment=True)

if __name__ == '__main__':
    application.run(host='0.0.0.0', port=5000, debug=True)
