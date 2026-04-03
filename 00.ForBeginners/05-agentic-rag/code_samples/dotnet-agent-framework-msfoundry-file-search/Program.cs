
using System.ClientModel;
using Azure.AI.Projects;
using Azure.AI.Projects.Agents;
using Azure.Identity;
using Microsoft.Agents.AI;
using Microsoft.Agents.AI.Foundry;
using OpenAI;
using OpenAI.Files;
using OpenAI.Responses;
using OpenAI.VectorStores;
using DotNetEnv;


Env.Load("../../../../.env");

var endpoint = Environment.GetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT") ?? throw new InvalidOperationException("FOUNDRY_PROJECT_ENDPOINT is not set.");
var deploymentName = Environment.GetEnvironmentVariable("FOUNDRY_MODEL") ?? "gpt-5.4";

// Create an AI Project client and get an OpenAI client that works with the foundry service.
AIProjectClient aiProjectClient = new(
    new Uri(endpoint),
    new AzureCliCredential());
OpenAIClient openAIClient = aiProjectClient.GetProjectOpenAIClient();

// Upload the file that contains the data to be used for RAG to the Foundry service.
OpenAIFileClient fileClient = openAIClient.GetOpenAIFileClient();
ClientResult<OpenAIFile> uploadResult = await fileClient.UploadFileAsync(
    filePath: "../document.md",
    purpose: FileUploadPurpose.Assistants);

#pragma warning disable OPENAI001
VectorStoreClient vectorStoreClient = openAIClient.GetVectorStoreClient();
ClientResult<VectorStore> vectorStoreCreate = await vectorStoreClient.CreateVectorStoreAsync(options: new VectorStoreCreationOptions()
{
    Name = "document-knowledge-base",
    FileIds = { uploadResult.Value.Id }
});
#pragma warning restore OPENAI001
#pragma warning disable OPENAI001
FileSearchTool fileSearchTool = new([vectorStoreCreate.Value.Id]);
#pragma warning restore OPENAI001

ProjectsAgentVersion agentVersion = await aiProjectClient.AgentAdministrationClient.CreateAgentVersionAsync(
    "dotNETRAGAgent",
    new ProjectsAgentVersionCreationOptions(
        new DeclarativeAgentDefinition(model: deploymentName)
        {
            Instructions = @"You are an AI assistant designed to answer user questions using only the information retrieved from the provided document(s). 
                If a user's question cannot be answered using the retrieved context, you must clearly respond: 
                'I'm sorry, but the uploaded document does not contain the necessary information to answer that question.' 
                Do not answer from general knowledge or reasoning. Do not make assumptions or generate hypothetical explanations. 
                For questions that do have relevant content in the document, respond accurately and cite the document explicitly.",
            Tools = { fileSearchTool }
        }));

#pragma warning disable OPENAI001
FoundryAgent agent = aiProjectClient.AsAIAgent(agentVersion);
#pragma warning restore OPENAI001

#pragma warning disable OPENAI001
AgentSession session = await agent.CreateSessionAsync();

Console.WriteLine(await agent.RunAsync("Can you explain Contoso's travel insurance coverage?", session));

