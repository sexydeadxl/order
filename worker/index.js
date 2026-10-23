export default {
  async fetch(request, env) {
    if (!env.DB_SECRET || request.headers.get('Authorization') !== `Bearer ${env.DB_SECRET}`)
      return new Response('Unauthorized', {status:401});
    const path = new URL(request.url).pathname;
    if (request.method !== 'POST' || !['/raw','/batch'].includes(path))
      return new Response('Not found', {status:404});
    try {
      if (path === '/batch') {
        const {statements} = await request.json();
        if (!Array.isArray(statements) || statements.length > 10) throw new Error('Invalid batch');
        const result = await env.DB.batch(statements.map(s => env.DB.prepare(s.sql).bind(...s.params)));
        return Response.json({success:true,result});
      }
      const {sql, params = []} = await request.json();
      if (typeof sql !== 'string' || !Array.isArray(params)) throw new Error('Invalid query');
      const statement = env.DB.prepare(sql).bind(...params);
      let columns = [], rows = [], meta = {};
      if (/^\s*(SELECT|PRAGMA|WITH|EXPLAIN)\b/i.test(sql)) {
        const result = await statement.raw({columnNames:true});
        columns = result[0] || []; rows = result.slice(1);
      } else {
        const result = await statement.run();
        meta = result.meta;
        columns = Object.keys(result.results?.[0] || {});
        rows = (result.results || []).map(row => columns.map(key => row[key]));
      }
      return Response.json({success:true,result:[{success:true,results:{columns,rows},meta}]});
    } catch (error) {
      console.error('D1 query failed', error.message);
      return Response.json({success:false,errors:[{message:'Database query failed'}]});
    }
  }
};
