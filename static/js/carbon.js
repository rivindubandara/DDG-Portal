import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js'
import { Rhino3dmLoader } from 'three/addons/loaders/3DMLoader.js';
import { GUI } from 'three/addons/libs/lil-gui.module.min.js';

let camera, scene, renderer;
let controls, gui;
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
  camera.position.set( 50, -150, 50 );

  scene = new THREE.Scene();

  const ambientLight = new THREE.AmbientLight(0xffffff, 0.5);
  scene.add(ambientLight);
  
  const directionalLight = new THREE.DirectionalLight( 0xffffff, 2 );
  directionalLight.position.set( 20, 40, 100);
  scene.add( directionalLight );

  const loader = new Rhino3dmLoader();
  loader.setLibraryPath( 'https://cdn.jsdelivr.net/npm/rhino3dm@7.15.0/' );
  loader.load( '/static/carbon_output.3dm', function ( object ) {

    scene.add( object );
    initGUI( object.userData.layers );

    // hide spinner
    document.getElementById( 'loader' ).style.display = 'none';

  } );

  controls = new OrbitControls( camera, renderer.domElement );
  controls.enableZoom = true;
  controls.enableDamping = true;
  controls.dampingDactor = 0.05;

  controls.minDistance = 100;
	controls.maxDistance = 1000;

  controls.maxPolarAngle = Math.PI / 2;

  window.addEventListener( 'resize', resize );
  
}

function resize() {

  const width = window.innerWidth;
  const height = window.innerHeight;

  camera.aspect = width / height;
  camera.updateProjectionMatrix();

  renderer.setSize( width, height );

}

function animate() {

  controls.update();
  renderer.render( scene, camera );
  renderer.setPixelRatio(2)
  requestAnimationFrame( animate );

}

function initGUI( layers ) {
  gui = new GUI({ 
  title: 'Layers',
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
