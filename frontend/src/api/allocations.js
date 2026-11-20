import {api} from './client.js';
export const allocationsApi={
  preview:(sampleId,data)=>api(`/samples/${sampleId}/allocations/preview`,'POST',data),
  detail:id=>api('/allocations/'+id),
  confirm:(id,request_key)=>api(`/allocations/${id}/confirm`,'POST',{request_key}),
  list:sampleId=>api(`/samples/${sampleId}/allocations`),
};
