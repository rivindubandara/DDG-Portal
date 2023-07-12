import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js'
import { Rhino3dmLoader } from 'three/addons/loaders/3DMLoader.js';
import { GUI } from 'three/addons/libs/lil-gui.module.min.js';

let camera, scene, renderer;
let camera2, scene2, renderer2;
let controls, gui;
let cube;

init();
animate();

function init() {

  THREE.Object3D.DEFAULT_UP.set( 0, 0, 1 );

  renderer = new THREE.WebGLRenderer( { antialias: true } );
  renderer.setPixelRatio( 2 );
  renderer.setSize( window.innerWidth, window.innerHeight );
  renderer.outputEncoding = THREE.sRGBEncoding;
  document.body.appendChild( renderer.domElement );

  camera = new THREE.PerspectiveCamera( 60, window.innerWidth / window.innerHeight, 1, 10000 );
  camera.position.set( 150, -70, 300 );

  scene = new THREE.Scene();

  const ambientLight = new THREE.AmbientLight(0xffffff, 0.5);
  scene.add(ambientLight);
  
  const directionalLight = new THREE.DirectionalLight( 0xffffff, 2 );
  directionalLight.position.set( 20, 40, 100);
  scene.add( directionalLight );

  const loader = new Rhino3dmLoader();
  loader.setLibraryPath( 'https://cdn.jsdelivr.net/npm/rhino3dm@7.15.0/' );
  loader.load( '/static/environmental.3dm', function ( object ) {

    scene.add( object );
    initGUI( object.userData.layers );

    document.getElementById( 'loader' ).style.display = 'none';

  } );

  controls = new OrbitControls( camera, renderer.domElement );
  controls.enableZoom = true;
  controls.enableDamping = true;
  controls.dampingDactor = 0.05;

  controls.minDistance = 100;
  controls.maxDistance = 1000;

  controls.maxPolarAngle = Math.PI / 2;

  // Secondary Scene
  scene2 = new THREE.Scene();
  camera2 = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
  camera2.position.z = 5; 
  renderer2 = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer2.setClearColor( 0x000000, 0 );
  renderer2.setPixelRatio( window.devicePixelRatio );
  renderer2.setSize( window.innerWidth / 4, window.innerHeight / 4 ); 
  document.body.appendChild( renderer2.domElement );
  renderer2.domElement.style.position = 'absolute';
  renderer2.domElement.style.bottom = '0px';
  renderer2.domElement.style.right = '0px';

  // Create a rotating cube (to be replaced by legend mesh)
  const geometry = new THREE.BoxGeometry(3, 3, 3);
  const material = new THREE.MeshBasicMaterial({ color: 0x41999c });
  cube = new THREE.Mesh(geometry, material);
  scene2.add(cube);

  window.addEventListener( 'resize', resize );
  
}

function resize() {

  const width = window.innerWidth;
  const height = window.innerHeight;

  camera.aspect = width / height;
  camera.updateProjectionMatrix();

  renderer.setSize( width, height );

  // secondary renderer
  camera2.aspect = (width / 4) / (height / 4);
  camera2.updateProjectionMatrix();
  renderer2.setSize( width / 4, height / 4);

}

function animate() {

  cube.rotation.x += 0.01;
  cube.rotation.y += 0.01;

  controls.update();
  renderer.render( scene, camera );
  renderer2.render( scene2, camera2 );
  requestAnimationFrame( animate );

}

function initGUI( layers ) {
  gui = new GUI({ 
  title: 'Layers',
  width: 250
  });

  gui.domElement.classList.add('gui-container');

  for ( let i = 0; i < layers.length; i ++ ) {

    const layer = layers[ i ];
    gui.add( layer, 'visible' ).name( layer.name ).onChange( function ( val ) {

      const name = this.object.name;

      scene.traverse( function ( child ) {

        if ( child.userData.hasOwnProperty( 'attributes' ) ) {

          if ( 'layerIndex' in child.userData.attributes ) {

            const layerName = layers[ child.userData.attributes.layerIndex ].name;

            if ( layerName === name ) {

              child.visible = val;
              layer.visible = val;

            }

          }

        }

      } );

    } );

  }

}
