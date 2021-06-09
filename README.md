# bathroom-map

## Installation
1. Clone this repository: `git clone https://github.com/saligrama/bathroom-map`
2. Install vagrant: https://www.vagrantup.com/downloads
3. Open a terminal in the `bathroom-map/vagrant` directory and run `vagrant up`
4. Run `vagrant ssh`
5. Once you have a terminal in the vagrant virtual machine, run `cd bathroom-map && python3 manage.py runserver 0.0.0.0:8000`
6. Map should load at `http://localhost:8080/map`.
7. Map markers can be added at `http://localhost:8080/admin`: click on the `Add` button next to `Bathrooms`, and enter the info. You can leave latitude/longitude blank or enter zeros, it'll autocompute it from your address and zip.