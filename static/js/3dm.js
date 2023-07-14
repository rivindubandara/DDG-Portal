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
  camera2.position.z = 220; 
  renderer2 = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer2.setClearColor( 0x000000, 0 );
  renderer2.setPixelRatio( window.devicePixelRatio );
  renderer2.setSize( window.innerWidth / 2, window.innerHeight / 2 ); 
  document.body.appendChild( renderer2.domElement );
  renderer2.domElement.style.position = 'absolute';
  renderer2.domElement.style.bottom = '-100px';
  renderer2.domElement.style.right = '-350px';

  const ambientLight2 = new THREE.AmbientLight(0xffffff, 0.5);
  scene2.add(ambientLight2);
  
  // Adding directional light to the secondary scene
  const directionalLight2 = new THREE.DirectionalLight(0xffffff, 2);
  directionalLight2.position.set(20, 40, 100);
  scene2.add(directionalLight2);

  // Load a different 3DM file for the second scene
  const loader2 = new Rhino3dmLoader();
  loader2.setLibraryPath( 'https://cdn.jsdelivr.net/npm/rhino3dm@7.15.0/' );
  loader2.load( '/static/environmental_2.3dm', function ( object ) {
    object.scale.set(4, 4, 4);
    scene2.add( object );
  } );

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
