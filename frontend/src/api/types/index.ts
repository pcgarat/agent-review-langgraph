/**
 * Tipos de la API, uno por recurso.
 *
 * Cada fichero pertenece a un recurso del contrato (§5.3 de la spec 029) para que
 * dos pantallas que se construyen a la vez no editen el mismo fichero. Los
 * imports concretos son la forma preferida; este barril existe para no obligar a
 * tres líneas de import cuando se usan varios.
 */

export type * from './session'
export type * from './state'
export type * from './review'
export type * from './history'
export type * from './bandeja'
export type * from './config'
